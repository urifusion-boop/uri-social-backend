import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.agents.social_media_manager.services import rate_limit as rl

GRAPH_BASE = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

# Poll limits for Instagram container status
_IG_POLL_INTERVAL = 8   # seconds between polls
_IG_MAX_POLLS = 90      # 90 × 8s = 12 minutes max



class MetaAPIError(ValueError):
    """A Meta Graph API call failed. Still a ValueError with the same message as before, so existing
    handling is unchanged; `.rate_limit` is set when Meta said it was throttling us."""

    def __init__(self, message: str, rate_limit: Optional["rl.RateLimitInfo"] = None) -> None:
        super().__init__(message)
        self.rate_limit = rate_limit


def _meta_error(prefix: str, resp, data, platform: str) -> "MetaAPIError":
    message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
    return MetaAPIError(f"{prefix}: {message}", rl.meta_limit_fields(resp, data, platform).get("rate_limit"))


def _jobs_col():
    return get_db()["video_publish_jobs"]


async def get_publish_job(job_id: str) -> Optional[Dict[str, Any]]:
    return await _jobs_col().find_one({"job_id": job_id}, {"_id": 0})


# What the app is allowed to see of a job. A whitelist, not "everything on the document", so adding
# internal bookkeeping fields (account keys, user ids) never leaks them through the status endpoint.
_PUBLIC_JOB_FIELDS = (
    "job_id", "draft_id", "platform", "status", "platform_post_id", "post_url", "error",
    "error_code", "retryable", "next_attempt_at", "resume_at", "wait_reason", "created_at", "updated_at",
)


def public_job_view(job: Dict[str, Any]) -> Dict[str, Any]:
    return {k: job[k] for k in _PUBLIC_JOB_FIELDS if k in job}


# A job is "alive" only while its background task is. If the worker restarts mid-publish the task is
# gone and nothing ever updates the job again — it would show "uploading" forever. A job normally
# touches `updated_at` whenever its status changes, and the longest legitimate stretch without a
# change (waiting for the account up to PUBLISH_JOB_WAIT_MAX_S, then an Outstand post polled for up
# to ~12 min) is far under this, so a job quiet for this long is dead.
STALE_JOB_MINUTES = 45


async def mark_stale_jobs(now: Optional[datetime] = None) -> int:
    """Fail jobs left "queued"/"uploading" by a worker that went away, saying so — and that trying
    again is fine — instead of leaving them spinning. Returns how many it marked."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=STALE_JOB_MINUTES)).isoformat()
    # `updated_at` is an ISO string on jobs (like `created_at`); jobs from before it existed fall
    # back to their creation time.
    result = await _jobs_col().update_many(
        {"status": {"$in": ["queued", "uploading"]},
         "$or": [{"updated_at": {"$lte": cutoff}},
                 {"updated_at": {"$exists": False}, "created_at": {"$lte": cutoff}}]},
        {"$set": {
            "status": "failed",
            "error": "This post was interrupted before it finished (the server restarted). "
                     "It may not have been sent — check the account, then try again.",
            "error_code": "interrupted",
            "retryable": True,
            "updated_at": now.isoformat(),
        }},
    )
    return getattr(result, "modified_count", 0) or 0


class VideoPublishService:

    @staticmethod
    async def create_job(draft_id: str, platform: str, user_id: str) -> str:
        job_id = uuid.uuid4().hex
        await _jobs_col().insert_one({
            "job_id": job_id,
            "user_id": user_id,
            "draft_id": draft_id,
            "platform": platform,
            "status": "queued",
            "platform_post_id": None,
            "post_url": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return job_id

    @staticmethod
    async def run_job(
        job_id: str,
        draft_id: str,
        platform: str,
        video_url: str,
        caption: str,
        conn: Dict[str, Any],
        db,
        tiktok_settings=None,
        instagram_audio=None,
    ) -> None:
        col = _jobs_col()
        key, gap, net = VideoPublishService._gate_target(platform, conn)
        try:
            async def publish():
                if platform == "instagram_reels":
                    return await VideoPublishService._publish_instagram_reel(
                        conn["ig_user_id"], conn["page_access_token"], video_url, caption, instagram_audio
                    )
                if platform == "facebook_reels":
                    return await VideoPublishService._publish_facebook_video(
                        conn["page_access_token"], video_url, caption
                    )
                if platform == "tiktok":
                    return await VideoPublishService._publish_tiktok(conn, video_url, caption, db, tiktok_settings)
                raise ValueError(f"Unsupported platform: {platform}")

            post_id, post_url = await VideoPublishService._with_gate(job_id, key, gap, net, publish)

            await col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "status": "published",
                    "platform_post_id": post_id,
                    "post_url": post_url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            # Mark the source draft as published
            await db["content_drafts"].update_one(
                {"id": draft_id},
                {"$set": {"status": "published", "published_date": datetime.now(timezone.utc).isoformat()}},
            )

        except Exception as e:
            print(f"[VideoPublish {job_id}] failed: {e}")
            await VideoPublishService._fail(job_id, e, net)

    # ── Rate limits: one post at a time per account, and say so when a limit stops one ──

    @staticmethod
    def _gate_target(platform: str, conn: Dict[str, Any]):
        """(gate key, minimum gap in seconds, network) for the account a job posts to, or a None key
        when we can't tell which account it is (then the job simply isn't gated). Job platform names
        (`instagram_reels`, `facebook_reels`) are mapped to the network."""
        network = {"instagram_reels": "instagram", "facebook_reels": "facebook"}.get(platform, platform)
        return rl.gate_target_for_connection(network, conn)

    @staticmethod
    async def _with_gate(job_id: str, key: Optional[str], min_gap_s: float, network: str, body):
        """Run body() as the only post in flight for this account, with the minimum gap before the
        next one. While it waits for the account the job is "queued" with the time it expects to go
        (`next_attempt_at`). If a rate limit stops the post, the account is paused so the next post
        doesn't walk into the same wall; after a clean post the account's backoff resets.

        The wait is bounded (PUBLISH_JOB_WAIT_MAX_S): a job that still can't go then fails with a
        message saying when to try again, rather than sitting "queued" forever."""
        col = _jobs_col()
        if not key:
            await col.update_one({"job_id": job_id}, {"$set": {"status": "uploading",
                                 "updated_at": datetime.now(timezone.utc).isoformat()}})
            return await body()

        cfg = rl.get_settings()
        db = get_db()
        owner = rl.new_owner()

        async def waiting(decision: "rl.GateDecision") -> None:
            await col.update_one({"job_id": job_id}, {"$set": {
                "status": "queued",
                "account_key": key,
                "wait_reason": "rate_limited" if decision.reason == "paused" else "account_busy",
                "next_attempt_at": decision.retry_at.isoformat() if decision.retry_at else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }})

        decision = await rl.wait_for(db, key, owner, max_wait_s=cfg.job_wait_max_s, lease_s=cfg.lease_s,
                                     wait_through_pause=True, on_wait=waiting)
        if not decision.granted:
            kind = rl.LimitKind.WINDOW if decision.reason == "paused" else rl.LimitKind.TEMPORARY
            info = rl.RateLimitInfo(kind=kind, platform=network, code=decision.pause_code or decision.reason,
                                    resume_at=decision.retry_at)
            raise rl.RateLimitedError(rl.give_up_message(info), info)

        try:
            await col.update_one({"job_id": job_id}, {
                "$set": {"status": "uploading", "account_key": key,
                         "updated_at": datetime.now(timezone.utc).isoformat()},
                "$unset": {"wait_reason": "", "next_attempt_at": ""},
            })
            result = await body()
            await rl.clear_backoff(db, key)
            return result
        except Exception as e:
            info = rl.classify_exception(e, network)
            if info and info.retryable:
                await rl.pause(db, key, until=info.resume_at or (
                    rl.utcnow() + timedelta(seconds=info.retry_after_s) if info.retry_after_s else None),
                    code=info.code, reason=str(e)[:300])
            raise
        finally:
            await rl.release(db, key, owner, min_gap_s=min_gap_s)

    @staticmethod
    async def _fail(job_id: str, exc: Exception, network: str = "tiktok") -> None:
        fields = VideoPublishService._failure_update(exc, network)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        await _jobs_col().update_one({"job_id": job_id}, {"$set": fields})

    @staticmethod
    def _failure_update(exc: Exception, network: str = "tiktok") -> Dict[str, Any]:
        """Job fields for an exception. A TikTok post that was accepted but hasn't
        reached a final state is "processing" (with the explanation), never
        "published" and not "failed" either — success needs TikTok's confirmation.

        A rate limit is a failure with its own `error_code` ("rate_limited" for a temporary limit
        or window, "daily_cap" / "app_cap" for caps) and `retryable`, so the app can say what
        happened and whether trying again helps, instead of showing a raw platform error."""
        from app.agents.social_media_manager.services.tiktok_direct_service import (
            TikTokStillProcessingError,
        )
        if isinstance(exc, TikTokStillProcessingError):
            return {"status": "processing", "error": str(exc)}
        info = rl.classify_exception(exc, network)
        if info:
            fields: Dict[str, Any] = {
                "status": "failed",
                "error": rl.give_up_message(info),
                "error_code": info.error_code,
                "retryable": info.retryable,
            }
            if info.resume_at:
                fields["resume_at"] = info.resume_at.isoformat()
            return fields
        from app.agents.social_media_manager.services.publish_confirmation import specific_message
        return {"status": "failed", "error": specific_message(network, str(exc)) or str(exc)}

    @staticmethod
    async def publish_scheduled_tiktok_photo(draft: Dict[str, Any], conn: Dict[str, Any], db) -> Dict[str, Any]:
        """Posts a scheduled TikTok image / carousel draft with the choices its owner made on the
        Post to TikTok screen when scheduling (draft["tiktok_settings"]). Called by the scheduler at
        the due time, which already holds the account's gate, so this neither gates nor records a job.

        Returns the result shape _publish_to_platform uses: success + post_id, or error (+ rate_limit)."""
        from app.agents.social_media_manager.services import tiktok_media_proxy
        from app.agents.social_media_manager.services.tiktok_direct_service import (
            TikTokPostSettings,
            TikTokStillProcessingError,
            get_valid_tiktok_access_token,
            publish_tiktok_photo_direct,
        )

        saved = draft.get("tiktok_settings") or {}
        try:
            tiktok_settings = TikTokPostSettings(**saved)
        except TypeError:
            return {"success": False, "error": "This scheduled TikTok post is missing its posting choices. Open it and schedule it again."}
        count = len(tiktok_media_proxy.draft_image_sources(draft))
        if count == 0:
            return {"success": False, "error": "This post has no image to publish."}
        image_urls = tiktok_media_proxy.signed_urls(draft["id"], count)
        auto_add_music = bool(draft.get("tiktok_auto_add_music"))

        try:
            if conn.get("connected_via") == "tiktok_direct_oauth":
                try:
                    access_token = await get_valid_tiktok_access_token(db, conn)
                    publish_id, status = await publish_tiktok_photo_direct(
                        access_token, image_urls, tiktok_settings, auto_add_music
                    )
                except TikTokStillProcessingError as e:
                    # TikTok has the post and is still working on it. Failing here would make the
                    # scheduler send it again, so record it as sent and let TikTok finish.
                    publish_id, status = getattr(e, "publish_id", None), "PROCESSING"
                    if not publish_id:
                        raise
                print(f"✅ Scheduled TikTok photo: publish_id={publish_id} status={status}")
                return {"success": True, "post_id": publish_id, "raw_response": {"publish_id": publish_id, "status": status}}

            return {"success": False, "needs_reconnect": True,
                    "error": "Reconnect your TikTok account on Connected Accounts, then try again."}
        except Exception as e:
            print(f"❌ Scheduled TikTok photo failed: {e}")
            failed: Dict[str, Any] = {"success": False, "error": f"TikTok publish failed: {e}"}
            info = rl.classify_exception(e, "tiktok")
            if info is not None:
                failed["rate_limit"] = info
            return failed

    @staticmethod
    async def run_tiktok_photo_job(
        job_id: str,
        draft_id: str,
        image_urls: list,
        conn: Dict[str, Any],
        db,
        tiktok_settings,
        auto_add_music: bool = False,
    ) -> None:
        """Direct-posts an image / carousel draft to TikTok, recording progress on
        the same job document the video flow uses, so the frontend polls both
        through /video-publish-job/{job_id}."""
        from app.agents.social_media_manager.services.tiktok_direct_service import (
            get_valid_tiktok_access_token,
            publish_tiktok_photo_direct,
        )

        col = _jobs_col()
        key, gap, net = VideoPublishService._gate_target("tiktok", conn)
        try:
            async def publish():
                access_token = await get_valid_tiktok_access_token(db, conn)
                return await publish_tiktok_photo_direct(access_token, image_urls, tiktok_settings, auto_add_music)

            publish_id, status = await VideoPublishService._with_gate(job_id, key, gap, net, publish)
            print(f"✅ TikTok direct photo publish: publish_id={publish_id} status={status}")

            await col.update_one(
                {"job_id": job_id},
                {"$set": {"status": "published", "platform_post_id": publish_id, "post_url": None,
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
            await db["content_drafts"].update_one(
                {"id": draft_id},
                {"$set": {
                    "status": "published",
                    "platform_post_id": publish_id,
                    "published_date": datetime.now(timezone.utc).isoformat(),
                }},
            )
        except Exception as e:
            print(f"[TikTokPhotoPublish {job_id}] failed: {e}")
            await VideoPublishService._fail(job_id, e, net)

    # ── Instagram Reels ───────────────────────────────────────────────────────

    @staticmethod
    async def _publish_instagram_reel(
        ig_user_id: str,
        page_access_token: str,
        video_url: str,
        caption: str,
        audio: Optional[Dict[str, Any]] = None,
    ):
        from app.agents.social_media_manager.services import instagram_audio

        container_params = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": page_access_token,
        }
        audio_cfg = instagram_audio.audio_configuration(audio)
        container_base = GRAPH_BASE
        if audio_cfg:
            container_params["audio_configuration"] = audio_cfg
            container_base = instagram_audio.AUDIO_BASE   # the music option is documented on a newer Graph version

        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: create media container
            create_resp = await client.post(f"{container_base}/{ig_user_id}/media", params=container_params)
            create_data = create_resp.json()
            if "error" in create_data:
                # Never quietly post the Reel without the track the customer chose.
                prefix = "IG container create error (a music track was attached)" if audio_cfg else "IG container create error"
                raise _meta_error(prefix, create_resp, create_data, "instagram")
            container_id = create_data["id"]

            # Step 2: poll until FINISHED
            for _ in range(_IG_MAX_POLLS):
                await asyncio.sleep(_IG_POLL_INTERVAL)
                status_resp = await client.get(
                    f"{GRAPH_BASE}/{container_id}",
                    params={"fields": "status_code,status", "access_token": page_access_token},
                )
                status_data = status_resp.json()
                # A hiccup on a status check is ignored (we keep polling), as before — but a rate
                # limit is not: more calls only lengthen the lockout, so stop and say so.
                if "error" in status_data:
                    limited = rl.meta_limit_fields(status_resp, status_data, "instagram")
                    if limited:
                        raise _meta_error("IG container status error", status_resp, status_data, "instagram")
                status_code = status_data.get("status_code", "")
                print(f"[IGPublish] container {container_id} status: {status_code}")
                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    raise ValueError(f"IG container processing failed: {status_data.get('status')}")
            else:
                raise TimeoutError("Instagram container processing timed out")

            # Step 3: publish
            publish_resp = await client.post(
                f"{GRAPH_BASE}/{ig_user_id}/media_publish",
                params={"creation_id": container_id, "access_token": page_access_token},
            )
            publish_data = publish_resp.json()
            if "error" in publish_data:
                raise _meta_error("IG publish error", publish_resp, publish_data, "instagram")

            media_id = publish_data["id"]
            post_url = f"https://www.instagram.com/p/{media_id}/"
            return media_id, post_url

    # ── Facebook Video ────────────────────────────────────────────────────────

    @staticmethod
    async def _publish_facebook_video(
        page_access_token: str,
        video_url: str,
        caption: str,
    ):
        async with httpx.AsyncClient(timeout=30) as client:
            # Derive the page_id from the page token
            me_resp = await client.get(
                f"{GRAPH_BASE}/me",
                params={"access_token": page_access_token, "fields": "id,name"},
            )
            me_data = me_resp.json()
            if "error" in me_data:
                raise _meta_error("FB /me error", me_resp, me_data, "facebook")
            page_id = me_data["id"]

            # Post as a video to the page feed (supports public URL via file_url)
            post_resp = await client.post(
                f"{GRAPH_BASE}/{page_id}/videos",
                params={
                    "file_url": video_url,
                    "description": caption,
                    "published": "true",
                    "access_token": page_access_token,
                },
            )
            post_data = post_resp.json()
            if "error" in post_data:
                raise _meta_error("FB video post error", post_resp, post_data, "facebook")

            video_id = post_data["id"]
            post_url = f"https://www.facebook.com/video/{video_id}"
            return video_id, post_url

    # ── TikTok (direct FILE_UPLOAD) ──

    @staticmethod
    async def _publish_tiktok(
        conn: Dict[str, Any],
        video_url: str,
        caption: str,
        db,
        tiktok_settings=None,
    ):
        if conn.get("connected_via") == "tiktok_direct_oauth":
            from app.agents.social_media_manager.services.tiktok_direct_service import (
                get_valid_tiktok_access_token,
                publish_tiktok_direct,
            )
            access_token = await get_valid_tiktok_access_token(db, conn)
            publish_id, status = await publish_tiktok_direct(access_token, video_url, caption, tiktok_settings)
            print(f"✅ TikTok direct publish: publish_id={publish_id} status={status}")
            return publish_id, None

        raise ValueError("Reconnect your TikTok account on Connected Accounts, then try again.")
