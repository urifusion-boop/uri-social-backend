import asyncio
import os
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.utils.s3_upload import upload_file_path

# Model lineup matches the "URI_AI_Video_API_Model_Selection_Engineering_Brief"
# (Uzuri Creative / URI, 1 October 2026) exactly — its 7 selected launch routes,
# minus Seedance 2.5 which the brief explicitly says not to make a launch
# default ("Do not expose as standard launch infrastructure without evidence
# that the quality uplift improves conversion or reduces retries enough to
# justify the cost" — §3.8). Every model here is fal.ai's own IMAGE-to-video
# endpoint for that model (confirmed per-model from fal.ai's own API docs,
# 2026-10-01) since this pipeline always has a real, brand-grounded storyboard
# frame to animate — never a bare text prompt.
#
# Two of the brief's picks (MiniMax H3 Max Turbo, MiniMax H3 Max) are listed
# under fal.ai's bare `minimax/` namespace rather than `fal-ai/` — confirmed
# correct, not a typo; that's how fal.ai itself hosts this specific partner's
# models.
#
# cost_per_second below is the brief's own figure (§1's table), converted from
# its stated NGN back to USD at the brief's own FX assumption (₦1,327.84/$1,
# 1 October 2026) so it's directly comparable with the rest of this file's USD
# math — not re-derived from a separate pricing lookup. The brief itself warns
# prices change and should be re-checked against live fal.ai pricing before any
# production rollout; this is a snapshot, not a live-fetched rate.
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "minimax/h3-max-turbo/image-to-video": {
        "label": "H3 Max Turbo",
        "cost_per_second": 0.0399,
    },
    "fal-ai/kling-video/v2.5-turbo/standard/image-to-video": {
        "label": "Kling 2.5 Standard",
        "cost_per_second": 0.0422,
    },
    "fal-ai/pixverse/v6/image-to-video": {
        "label": "PixVerse V6",
        "cost_per_second": 0.0603,
    },
    "minimax/h3-max/image-to-video": {
        "label": "H3 Max",
        "cost_per_second": 0.0798,
    },
    "alibaba/wan-3.0/image-to-video": {
        "label": "Wan 3.0",
        "cost_per_second": 0.1002,
    },
    "fal-ai/veo3.1/fast/image-to-video": {
        "label": "Veo 3.1 Fast",
        "cost_per_second": 0.1499,
    },
    "bytedance/seedance-2.0/image-to-video": {
        "label": "Seedance 2.0 Fast",
        "cost_per_second": 0.2419,
    },
    # Not in the brief (which predates this need) — added for real talking-
    # head support. Veo 3.1 Fast's "generate_audio" only produces ambient/
    # prompt-described audio, not a script the caller actually controls, and
    # isn't true lip-sync. fal-ai/ai-avatar/single-text takes an exact script
    # (text_input), converts it to speech (voice enum), and lip-syncs a still
    # image to it — confirmed via fal.ai's own API docs, 2026-10-01. Priced
    # at $0.2/sec at 480p ("$0.2 per second... 720p price will be doubled" —
    # fal.ai's own pricing copy); this registry entry fixes 480p so cost stays
    # predictable (see _generate_scene_fal).
    "fal-ai/ai-avatar/single-text": {
        "label": "Talking Avatar",
        "cost_per_second": 0.2,
    },
}

_H3_TURBO = "minimax/h3-max-turbo/image-to-video"
_KLING_STANDARD = "fal-ai/kling-video/v2.5-turbo/standard/image-to-video"
_PIXVERSE = "fal-ai/pixverse/v6/image-to-video"
_H3_MAX = "minimax/h3-max/image-to-video"
_WAN_3 = "alibaba/wan-3.0/image-to-video"
_VEO_FAST = "fal-ai/veo3.1/fast/image-to-video"
_SEEDANCE_FAST = "bytedance/seedance-2.0/image-to-video"
_AVATAR_TALKING = "fal-ai/ai-avatar/single-text"

# fal.ai's own documented voice enum for fal-ai/ai-avatar/single-text — keep
# in sync with AVATAR_VOICES in VideoStoryboardGenerator.tsx if this changes.
AVATAR_VOICES = [
    "Aria", "Roger", "Sarah", "Laura", "Charlie", "George", "Callum", "River",
    "Liam", "Charlotte", "Alice", "Matilda", "Will", "Jessica", "Eric",
    "Chris", "Brian", "Daniel", "Lily", "Bill",
]
DEFAULT_AVATAR_VOICE = "Sarah"

# §2 ("Product Principle: URI Chooses the Model") + §4 ("Recommended URI
# Routing Logic") of the brief, combined: the customer picks an OUTCOME, never
# a raw model name — "Model names should remain an implementation detail so
# engineering can change routing as pricing, quality and availability change."
# Each outcome's fallback is §4's own table entry for that same request
# signal, used automatically (not offered as a choice) when the primary route
# fails — see run_job below. label/description are what the UI shows; the
# model ids are never surfaced to a customer-facing caller.
OUTCOME_ROUTES: Dict[str, Dict[str, Any]] = {
    "quick_video": {
        "label": "Quick Video",
        "description": "Fast, low-cost general creative — the everyday default.",
        "primary": _H3_TURBO,
        "fallback": _PIXVERSE,
    },
    "animate_product": {
        "label": "Animate My Product",
        "description": "Turn a product/brand photo into camera movement.",
        "primary": _KLING_STANDARD,
        "fallback": _H3_TURBO,
    },
    "social_video": {
        "label": "Social Video",
        "description": "Cheap variants for iteration — native audio, high volume.",
        "primary": _PIXVERSE,
        "fallback": _H3_TURBO,
    },
    "high_quality": {
        "label": "High Quality",
        "description": "Premium brand shots and hero creative.",
        "primary": _H3_MAX,
        "fallback": _WAN_3,
    },
    "complex_cinematic": {
        "label": "Complex / Cinematic",
        "description": "Hard motion, multiple subjects, demanding scene coherence.",
        "primary": _WAN_3,
        "fallback": _H3_MAX,
    },
    "talking_dialogue": {
        "label": "Talking / Dialogue",
        "description": "Synchronized speech, lip-sync, a visible talking human.",
        # Primary is the dedicated talking-avatar model (exact scripted
        # dialogue + real lip-sync), not Veo — Veo's audio is prompt-described,
        # not a script the caller controls. Veo stays as fallback: it still
        # produces a plausible talking-human clip (its own prompt can describe
        # speech) if the avatar model fails, which beats a hard failure.
        "primary": _AVATAR_TALKING,
        "fallback": _VEO_FAST,
    },
    "advanced_references": {
        "label": "Advanced References",
        "description": "Sophisticated reference-driven, brand-consistency-critical creative.",
        "primary": _SEEDANCE_FAST,
        "fallback": _H3_MAX,
    },
}

DEFAULT_OUTCOME = "quick_video"

# Per-attempt ceiling on fal.ai's subscribe_async (see _generate_scene_fal) —
# generous enough for the slower models (avatar/lip-sync, Seedance) without
# letting a genuine hang block a scene (and the background task behind it)
# forever. A timeout here is treated exactly like any other primary-model
# failure: run_job retries once on the outcome's paired fallback.
_FAL_SUBSCRIBE_TIMEOUT_SECONDS = 300


def _jobs_collection():
    return get_db()["video_generation_jobs"]


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    doc = await _jobs_collection().find_one({"job_id": job_id}, {"_id": 0})
    return doc


class VideoGenerationService:

    @staticmethod
    async def create_job(storyboard: dict, outcome: str, avatar_voice: str = DEFAULT_AVATAR_VOICE) -> str:
        job_id = uuid.uuid4().hex
        route = OUTCOME_ROUTES.get(outcome, OUTCOME_ROUTES[DEFAULT_OUTCOME])
        await _jobs_collection().insert_one({
            "job_id": job_id,
            "status": "queued",
            "outcome": outcome,
            "model": route["primary"],  # kept for display/back-compat; the real model per clip is `routed_model`
            "avatar_voice": avatar_voice,  # only consulted when a clip routes to _AVATAR_TALKING
            "total_scenes": len(storyboard.get("scenes", [])),
            "current_scene": 0,
            "clips": [],
            "error": None,
        })
        return job_id

    @staticmethod
    async def run_job(
        job_id: str,
        storyboard: dict,
        brand_images: List[str],
        outcome: str,
        avatar_voice: str = DEFAULT_AVATAR_VOICE,
    ) -> None:
        col = _jobs_collection()
        scenes = storyboard.get("scenes", [])
        route = OUTCOME_ROUTES.get(outcome, OUTCOME_ROUTES[DEFAULT_OUTCOME])
        primary_model, fallback_model = route["primary"], route["fallback"]
        if avatar_voice not in AVATAR_VOICES:
            avatar_voice = DEFAULT_AVATAR_VOICE
        await col.update_one({"job_id": job_id}, {"$set": {"status": "generating"}})

        for scene in scenes:
            scene_num = scene.get("scene_number", 0)
            await col.update_one({"job_id": job_id}, {"$set": {"current_scene": scene_num}})

            base_fields = {
                "scene_number": scene_num,
                "shot_type": scene.get("shot_type", ""),
                "duration_seconds": scene.get("duration_seconds", 5),
                "motion": scene.get("motion", ""),
                "text_overlay": scene.get("text_overlay"),
                "video_prompt": scene.get("video_prompt", ""),
            }

            # §6 ("Reliability & fallback"): "Fallback to the nearest lower-cost/
            # quality-compatible route where possible" + "Record whether fallback
            # altered expected quality, audio or reference support." One retry on
            # the outcome's own paired fallback model (§4's table), never a blind
            # retry of the same failing endpoint — if the fallback also fails,
            # that's a real, surfaced failure, not a silent third attempt.
            started = time.monotonic()
            fallback_used = False
            fallback_warning = None
            try:
                video_url, cost_usd = await VideoGenerationService._generate_scene_fal(
                    scene, primary_model, avatar_voice
                )
                routed_model = primary_model
            except Exception as primary_error:
                print(f"[VideoGenJob {job_id}] Scene {scene_num}: {primary_model} failed "
                      f"({primary_error}), falling back to {fallback_model}")
                # Record WHY it fell back even when the fallback itself succeeds —
                # a clip that "works" on the fallback can still be materially
                # different from what was asked for (e.g. talking_dialogue's
                # fallback has no scripted-speech guarantee at all), and without
                # this the UI shows an identical "Done" badge either way, hiding
                # that mismatch from the caller entirely.
                fallback_warning = f"Fell back to {MODEL_REGISTRY.get(fallback_model, {}).get('label', fallback_model)}: {primary_model} failed ({primary_error})"
                try:
                    video_url, cost_usd = await VideoGenerationService._generate_scene_fal(
                        scene, fallback_model, avatar_voice
                    )
                    routed_model = fallback_model
                    fallback_used = True
                except Exception as fallback_error:
                    latency = round(time.monotonic() - started, 2)
                    print(f"[VideoGenJob {job_id}] Scene {scene_num}: fallback {fallback_model} "
                          f"also failed ({fallback_error})")
                    await col.update_one({"job_id": job_id}, {"$push": {"clips": {
                        **base_fields,
                        "video_url": None,
                        "cost_usd": None,
                        "outcome": outcome,
                        "routed_model": None,
                        "fallback_used": True,
                        "latency_seconds": latency,
                        "error": f"{primary_model}: {primary_error} | fallback {fallback_model}: {fallback_error}",
                    }}})
                    continue

            latency = round(time.monotonic() - started, 2)
            await col.update_one({"job_id": job_id}, {"$push": {"clips": {
                **base_fields,
                "video_url": video_url,
                "cost_usd": cost_usd,
                "outcome": outcome,
                "routed_model": routed_model,
                "fallback_used": fallback_used,
                "warning": fallback_warning,
                "latency_seconds": latency,
            }}})

        await col.update_one(
            {"job_id": job_id},
            {"$set": {"status": "complete", "current_scene": len(scenes)}},
        )

    # ── fal.ai — every model in MODEL_REGISTRY goes through this one entry
    #    point. Each model's request shape genuinely differs (confirmed from
    #    fal.ai's own per-model API docs, 2026-10-01) — Wan 3.0 uniquely uses
    #    `start_image_url` where every other model here uses `image_url`;
    #    Kling 2.5 Standard only accepts duration 5 or 10; the two MiniMax H3
    #    models have no resolution/audio toggle beyond a 480P/768P/1080P enum
    #    (audio is automatic, not optional) — so this dispatches per model id
    #    rather than pretending they share one shape. ───────────────────────

    @staticmethod
    async def _generate_scene_fal(
        scene: dict, model: str, avatar_voice: str = DEFAULT_AVATAR_VOICE
    ) -> tuple[str, Optional[float]]:
        import fal_client

        frame_image_url = scene.get("frame_image_url")
        if not frame_image_url:
            raise ValueError(f"{model} requires a storyboard frame image — generate the storyboard frames first")

        info = MODEL_REGISTRY.get(model)
        if info is None:
            raise ValueError(f"Unknown video model: {model}")

        prompt = scene.get("video_prompt", "")
        duration_req = scene.get("duration_seconds", 5)
        scene_num = scene.get("scene_number")
        actual_seconds: float

        if model in (_H3_TURBO, _H3_MAX):
            # No documented min/max on `duration` beyond "integer, default 5" —
            # clamped to the 5-10s range every other MiniMax model in this file
            # uses, rather than sending an unvalidated raw value.
            seconds = max(5, min(10, duration_req))
            actual_seconds = seconds
            arguments: Dict[str, Any] = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,
                "resolution": "768P",
                "prompt_expansion_mode": "balanced",
            }
            print(f"[VideoGen] Scene {scene_num}: {info['label']}, {seconds}s")

        elif model == _KLING_STANDARD:
            seconds = 10 if duration_req >= 8 else 5  # only valid values per this model's own schema
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,
            }
            print(f"[VideoGen] Scene {scene_num}: Kling 2.5 Standard, {seconds}s")

        elif model == _PIXVERSE:
            seconds = max(1, min(15, duration_req))
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,
                "resolution": "720p",
                "generate_audio_switch": True,  # brief's own ₦80/sec figure is priced "720p + audio"
            }
            print(f"[VideoGen] Scene {scene_num}: PixVerse V6, {seconds}s")

        elif model == _WAN_3:
            seconds = max(3, min(10, duration_req))
            actual_seconds = seconds
            arguments = {
                "start_image_url": frame_image_url,  # NOT image_url — this model's own field name
                "prompt": prompt,
                "duration": seconds,
                "resolution": "720p",
                "aspect_ratio": "9:16",
            }
            print(f"[VideoGen] Scene {scene_num}: Wan 3.0, {seconds}s")

        elif model == _VEO_FAST:
            seconds = 8 if duration_req >= 7 else (6 if duration_req >= 5 else 4)
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": f"{seconds}s",
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "generate_audio": True,  # brief's own ₦199/sec figure is priced "Fast + audio"
            }
            print(f"[VideoGen] Scene {scene_num}: Veo 3.1 Fast, {seconds}s")

        elif model == _SEEDANCE_FAST:
            seconds = max(4, min(15, duration_req))
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": str(seconds),
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "generate_audio": True,
            }
            print(f"[VideoGen] Scene {scene_num}: Seedance 2.0 Fast, {seconds}s")

        elif model == _AVATAR_TALKING:
            # No duration field on this model — it's driven by num_frames at a
            # fixed internal frame rate. fal.ai doesn't publish the fps for
            # this endpoint; 25fps is the standard rate for the MultiTalk-
            # family avatar models this one is built on, used here as the
            # best-available estimate — if a live generation's actual runtime
            # diverges noticeably from this, re-derive from a real response.
            fps = 25
            num_frames = max(41, min(721, round(duration_req * fps)))
            actual_seconds = round(num_frames / fps, 2)
            dialogue = (scene.get("dialogue") or scene.get("text_overlay") or prompt or "").strip()
            if not dialogue:
                raise ValueError("Talking Avatar requires a scripted line — this scene has no dialogue")
            arguments = {
                "image_url": frame_image_url,
                "text_input": dialogue,
                "voice": avatar_voice if avatar_voice in AVATAR_VOICES else DEFAULT_AVATAR_VOICE,
                "prompt": prompt or dialogue,
                "num_frames": num_frames,
                "resolution": "480p",  # fixed — 720p doubles the $/sec rate baked into MODEL_REGISTRY above
            }
            print(f"[VideoGen] Scene {scene_num}: Talking Avatar ({avatar_voice}), ~{actual_seconds}s")

        else:
            raise ValueError(f"Unknown video model: {model}")

        # fal_client reads FAL_KEY; map our FAL_API_KEY if needed
        fal_key = settings.FAL_API_KEY
        if fal_key:
            os.environ["FAL_KEY"] = fal_key

        try:
            # subscribe_async has no timeout of its own — confirmed live
            # 2026-10-01: a job silently sat inside this call for minutes with
            # no error and no progress. Bounding it means a genuine hang (as
            # opposed to a slow-but-working queue) fails cleanly and fast
            # enough to trigger run_job's existing primary→fallback retry,
            # instead of parking a scene — and the whole job behind it —
            # indefinitely.
            result = await asyncio.wait_for(
                fal_client.subscribe_async(model, arguments), timeout=_FAL_SUBSCRIBE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"{model} did not respond within {_FAL_SUBSCRIBE_TIMEOUT_SECONDS}s"
            )
        except Exception as e:
            err_str = str(e)
            has_audio_toggle = "generate_audio" in arguments or "generate_audio_switch" in arguments
            if has_audio_toggle and (
                "content_policy_violation" in err_str or "sensitive content" in err_str.lower()
            ):
                print(f"[VideoGen] Scene {scene_num}: audio flagged, retrying without audio")
                toggle_key = "generate_audio" if "generate_audio" in arguments else "generate_audio_switch"
                arguments = {**arguments, toggle_key: False}
                result = await asyncio.wait_for(
                    fal_client.subscribe_async(model, arguments), timeout=_FAL_SUBSCRIBE_TIMEOUT_SECONDS
                )
            else:
                raise

        video_url = result["video"]["url"]

        # Stream the download straight to a temp file and upload FROM that
        # file (upload_file_path streams it in chunks too) — never holds the
        # whole clip in memory at once, unlike the old `.content` + bytes
        # upload. Confirmed live: that pattern was large and frequent enough,
        # combined with glibc not returning freed memory to the OS inside
        # the container, to ratchet process memory up until the kernel
        # OOM-killed a uvicorn worker. See upload_file_path's docstring.
        tmp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}.mp4")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("GET", video_url) as video_resp:
                    video_resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        async for chunk in video_resp.aiter_bytes():
                            f.write(chunk)

            stored_url = await upload_file_path(
                tmp_path,
                folder="uri-social/generated-videos",
                resource_type="video",
            )
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        cost_usd = round(actual_seconds * info["cost_per_second"], 3)
        return stored_url, cost_usd
