import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.agents.social_media_manager.services.outstand_service import OutstandService
from app.agents.social_media_manager.services import rate_limit as rl

GRAPH_BASE = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

# Poll limits for Instagram container status
_IG_POLL_INTERVAL = 8   # seconds between polls
_IG_MAX_POLLS = 90      # 90 × 8s = 12 minutes max

# Poll limits for Outstand post status (used for TikTok, which has no direct API)
_OUTSTAND_POLL_INTERVAL = 8   # seconds between polls
_OUTSTAND_MAX_POLLS = 90      # 90 × 8s = 12 minutes max


def outstand_tiktok_config(settings_=None, auto_add_music: bool = False, photo: bool = False) -> Dict[str, Any]:
    """The `tiktok` block of Outstand's POST /v1/posts/, built from the user's choices on
    the Post to TikTok screen (so a TikTok post is never sent with a privacy level or
    interaction the user didn't pick).

    postMode, privacyLevel and autoAddMusic are field names confirmed against live
    Outstand responses. disable*/brand* follow the same camelCase convention but are NOT
    yet confirmed against Outstand's schema — Outstand reports unrecognised keys in the
    response `warnings`, which log_outstand_warnings() makes visible. Photos omit
    Duet/Stitch (they don't apply). With no settings (legacy callers) the old default is
    kept."""
    if settings_ is None:
        cfg: Dict[str, Any] = {"postMode": "DIRECT_POST", "privacyLevel": "PUBLIC_TO_EVERYONE"}
    else:
        cfg = {
            "postMode": "DIRECT_POST",
            "privacyLevel": settings_.privacy_level,
            "disableComment": not settings_.allow_comment,
        }
        if not photo:
            cfg["disableDuet"] = not settings_.allow_duet
            cfg["disableStitch"] = not settings_.allow_stitch
        if settings_.brand_organic or settings_.brand_content:
            cfg["brandOrganicToggle"] = settings_.brand_organic
            cfg["brandContentToggle"] = settings_.brand_content
    if auto_add_music:
        cfg["autoAddMusic"] = True
    return cfg


def log_outstand_warnings(result: Dict[str, Any]) -> None:
    warnings = (result or {}).get("warnings")
    if warnings:
        print(f"⚠️ Outstand reported warnings for this TikTok post (settings it didn't recognise are ignored): {warnings}")


def explain_outstand_tiktok_failure(reason: Optional[str]) -> str:
    """A customer-readable explanation for a TikTok failure reported through Outstand."""
    low = (reason or "").lower()
    if "url_ownership_unverified" in low:
        return (
            "TikTok couldn't fetch the media: it is hosted on Outstand's domain, which isn't verified for the "
            "TikTok app this account was connected with. Use Outstand's own TikTok app (remove your TikTok "
            "credentials under Social Networks in the Outstand dashboard, then reconnect) or connect TikTok "
            f"with TikTok login instead. [{reason}]"
        )
    return f"TikTok publish failed via Outstand: {reason or 'unknown reason'}"


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
        return {"status": "failed", "error": str(exc)}

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

            outstand = OutstandService()
            media_urls = [await outstand.upload_media_from_url(u) for u in image_urls]
            caption = tiktok_settings.description or tiktok_settings.title or ""
            post_id, post_url = await VideoPublishService._publish_tiktok_via_outstand(
                conn["outstand_account_id"], caption, media_urls, tiktok_settings, auto_add_music, photo=True
            )
            return {"success": True, "post_id": post_id, "raw_response": {"post_id": post_id, "post_url": post_url}}
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

    @staticmethod
    async def run_tiktok_photo_job_outstand(
        job_id: str,
        draft_id: str,
        image_urls: list,
        conn: Dict[str, Any],
        db,
        tiktok_settings,
        auto_add_music: bool = False,
    ) -> None:
        """Same job as run_tiktok_photo_job, for a TikTok account connected through
        Outstand: the images are handed to Outstand, which posts them with the user's
        choices from the Post to TikTok screen."""
        col = _jobs_col()
        key, gap, net = VideoPublishService._gate_target("tiktok", conn)
        try:
            async def publish():
                outstand = OutstandService()
                media_urls = [await outstand.upload_media_from_url(u) for u in image_urls]
                caption = tiktok_settings.description or tiktok_settings.title or ""
                return await VideoPublishService._publish_tiktok_via_outstand(
                    conn["outstand_account_id"], caption, media_urls, tiktok_settings, auto_add_music, photo=True
                )

            post_id, post_url = await VideoPublishService._with_gate(job_id, key, gap, net, publish)
            await col.update_one(
                {"job_id": job_id},
                {"$set": {"status": "published", "platform_post_id": post_id, "post_url": post_url,
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
            await db["content_drafts"].update_one(
                {"id": draft_id},
                {"$set": {
                    "status": "published",
                    "platform_post_id": post_id,
                    "published_date": datetime.now(timezone.utc).isoformat(),
                }},
            )
        except Exception as e:
            print(f"[TikTokPhotoPublishOutstand {job_id}] failed: {e}")
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

    # ── TikTok (direct FILE_UPLOAD when connected that way, else via Outstand) ──

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

        outstand = OutstandService()

        # TikTok requires media to be fetched from a domain verified in the TikTok
        # developer portal. Route it through Outstand's own media upload first so
        # TikTok pulls from Outstand's (already-verified) domain instead.
        outstand_media_url = await outstand.upload_media_from_url(video_url)
        return await VideoPublishService._publish_tiktok_via_outstand(
            conn["outstand_account_id"], caption, [outstand_media_url], tiktok_settings
        )

    @staticmethod
    async def _publish_tiktok_via_outstand(
        outstand_account_id: str,
        caption: str,
        media_urls: list,
        tiktok_settings=None,
        auto_add_music: bool = False,
        photo: bool = False,
    ):
        """Posts to a TikTok account connected through Outstand and waits for the real
        outcome. Shared by the video and photo routes."""
        outstand = OutstandService()

        # DIRECT_POST + PUBLIC_TO_EVERYONE was believed correct per an earlier
        # note claiming the app's Content Posting API audit passed 2026-08-17.
        # Live-confirmed 2026-10-16 that's wrong (or no longer true): Outstand's
        # POST /v1/posts still returns 200 success:true and accepts the post —
        # TikTok's own "unaudited_client_can_only_post_to_private_accounts"
        # rejection only shows up later, asynchronously, through the status
        # poll below. So it can never be caught as an exception around
        # publish_post() (an earlier version of this tried that and the retry
        # never fired); it has to be handled after polling confirms the
        # failure, and recovering means submitting a brand-new post — the
        # original post_id is already dead on Outstand's side.
        tiktok_config = outstand_tiktok_config(tiktok_settings, auto_add_music, photo)
        # A 429 on creating the post means Outstand refused it before creating anything, so a
        # temporary limit is safe to retry here (it cannot double-post).
        result = await rl.with_backoff(
            lambda: outstand.publish_post(
                outstand_account_ids=[outstand_account_id],
                content=caption,
                media_urls=media_urls,
                platform_config={"tiktok": tiktok_config},
            ),
            classify=lambda e: rl.classify_exception(e, "tiktok"),
        )
        log_outstand_warnings(result)
        post_obj = result.get("post") or result.get("data") or {}
        if isinstance(post_obj, list):
            post_obj = post_obj[0] if post_obj else {}
        post_id = post_obj.get("id")
        if not post_id:
            raise ValueError(f"Outstand did not return a post id for TikTok publish: {result}")
        post_url = post_obj.get("url") or post_obj.get("permalink")

        published, post_url, failure_reason = await VideoPublishService._poll_tiktok_outstand_publish(
            outstand, post_id, post_url
        )

        # The media domain not being verified is its own problem (fixed by using Outstand's
        # own TikTok app, not by waiting for an audit) — say so, and don't fall back.
        if not published and failure_reason and "url_ownership_unverified" in failure_reason.lower():
            raise ValueError(explain_outstand_tiktok_failure(failure_reason))

        # A rate limit or cap is not a reason to submit more posts: every extra one would spend the
        # same limit, and the fallbacks below would just add noise. Say what happened instead.
        limit = rl.classify_text("tiktok", failure_reason) if (not published and failure_reason) else None
        if limit:
            raise rl.RateLimitedError(rl.give_up_message(limit), limit)

        audit_block = bool(failure_reason) and (
            "privacy" in failure_reason.lower() or "unaudited" in failure_reason.lower() or "audit" in failure_reason.lower()
        )

        # Only for callers with no user-chosen settings: quietly retry privately. When the
        # user picked a privacy level on the Post to TikTok screen it is never downgraded
        # behind their back — they get the explanation below instead.
        if not published and audit_block and tiktok_settings is None:
            print(f"⚠️ TikTok post {post_id} failed on privacyLevel/audit ({failure_reason}) — submitting a new post as SELF_ONLY")
            retry_result = await outstand.publish_post(
                outstand_account_ids=[outstand_account_id],
                content=caption,
                media_urls=media_urls,
                platform_config={"tiktok": {**tiktok_config, "privacyLevel": "SELF_ONLY"}},
            )
            retry_post_obj = retry_result.get("post") or retry_result.get("data") or {}
            if isinstance(retry_post_obj, list):
                retry_post_obj = retry_post_obj[0] if retry_post_obj else {}
            retry_post_id = retry_post_obj.get("id")
            if retry_post_id:
                post_id = retry_post_id
                post_url = retry_post_obj.get("url") or retry_post_obj.get("permalink")
                published, post_url, failure_reason = await VideoPublishService._poll_tiktok_outstand_publish(
                    outstand, post_id, post_url
                )
                audit_block = bool(failure_reason) and (
                    "privacy" in failure_reason.lower() or "unaudited" in failure_reason.lower() or "audit" in failure_reason.lower()
                )

        # Live-confirmed 2026-10-16: SELF_ONLY can fail with the exact same
        # "unaudited_client_can_only_post_to_private_accounts" error — the real
        # block is DIRECT_POST itself (likely under TikTok re-review), not the
        # privacy value. Fall back to TikTok's third suggested option,
        # MEDIA_UPLOAD, which drops privacyLevel and delivers to the creator's
        # TikTok inbox as a draft instead of posting directly.
        if not published and audit_block:
            print(f"⚠️ TikTok post {post_id} still failed on privacyLevel/audit ({failure_reason}) — submitting a new post as MEDIA_UPLOAD")
            media_result = await outstand.publish_post(
                outstand_account_ids=[outstand_account_id],
                content=caption,
                media_urls=media_urls,
                platform_config={"tiktok": {"postMode": "MEDIA_UPLOAD"}},
            )
            media_post_obj = media_result.get("post") or media_result.get("data") or {}
            if isinstance(media_post_obj, list):
                media_post_obj = media_post_obj[0] if media_post_obj else {}
            media_post_id = media_post_obj.get("id")
            if media_post_id:
                # A draft that TikTok rejected (e.g. media domain not verified) was never
                # delivered, so check it briefly before saying it was.
                _, _, media_failure = await VideoPublishService._poll_tiktok_outstand_publish(
                    outstand, media_post_id, None, max_polls=8
                )
                if media_failure:
                    raise ValueError(explain_outstand_tiktok_failure(media_failure))
                # MEDIA_UPLOAD never sets publishedAt until the creator manually
                # finishes it in the TikTok app — not a real publish, so raise
                # instead of returning a success tuple, with a clear explanation.
                raise ValueError(
                    "TikTok is currently blocking direct posting for this app (likely under review). "
                    f"This was delivered to the connected TikTok account's inbox as a draft (post {media_post_id}) "
                    "instead — open the TikTok app to review and publish it manually."
                )

        if not published:
            if failure_reason:
                raise ValueError(explain_outstand_tiktok_failure(failure_reason))
            raise TimeoutError("TikTok publish via Outstand timed out")

        return post_id, post_url

    @staticmethod
    async def _poll_tiktok_outstand_publish(
        outstand: "OutstandService", post_id: str, post_url: Optional[str], max_polls: Optional[int] = None
    ):
        """Poll Outstand until a TikTok post is confirmed published or fails.

        Returns (published, post_url, failure_reason). See the caller for why
        this can't be caught synchronously around publish_post().
        """
        for _ in range(max_polls or _OUTSTAND_MAX_POLLS):
            await asyncio.sleep(_OUTSTAND_POLL_INTERVAL)
            try:
                post_data = await outstand.get_post(post_id)
            except httpx.HTTPStatusError as e:
                # The post is already created: Outstand telling us to slow down on a status check
                # must not fail it. Wait it out (a missed poll) and carry on.
                if e.response is not None and e.response.status_code == 429:
                    await asyncio.sleep(min(rl.parse_retry_after(e.response.headers) or _OUTSTAND_POLL_INTERVAL * 2, 60.0))
                    continue
                raise
            post = post_data.get("post") or post_data.get("data") or {}
            if isinstance(post, list):
                post = post[0] if post else {}
            if post.get("publishedAt") and not post.get("isDraft"):
                return True, (post.get("url") or post.get("permalink") or post_url), None
            # The failure reason lives per-account, not on the post itself —
            # live-confirmed via Outstand's own dashboard: socialAccounts[].status
            # == "failed" with socialAccounts[].error holding the real detail.
            failed_acct = next(
                (a for a in (post.get("socialAccounts") or []) if a.get("status") == "failed"),
                None,
            )
            if failed_acct:
                return False, post_url, (failed_acct.get("error") or "unknown reason")
        return False, post_url, None
