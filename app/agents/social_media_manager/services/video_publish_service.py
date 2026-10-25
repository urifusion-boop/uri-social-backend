import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.agents.social_media_manager.services.outstand_service import OutstandService

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


def _jobs_col():
    return get_db()["video_publish_jobs"]


async def get_publish_job(job_id: str) -> Optional[Dict[str, Any]]:
    return await _jobs_col().find_one({"job_id": job_id}, {"_id": 0})


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
    ) -> None:
        col = _jobs_col()
        try:
            await col.update_one({"job_id": job_id}, {"$set": {"status": "uploading"}})

            if platform == "instagram_reels":
                post_id, post_url = await VideoPublishService._publish_instagram_reel(
                    conn["ig_user_id"], conn["page_access_token"], video_url, caption
                )
            elif platform == "facebook_reels":
                post_id, post_url = await VideoPublishService._publish_facebook_video(
                    conn["page_access_token"], video_url, caption
                )
            elif platform == "tiktok":
                post_id, post_url = await VideoPublishService._publish_tiktok(
                    conn, video_url, caption, db, tiktok_settings
                )
            else:
                raise ValueError(f"Unsupported platform: {platform}")

            await col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "status": "published",
                    "platform_post_id": post_id,
                    "post_url": post_url,
                }},
            )
            # Mark the source draft as published
            await db["content_drafts"].update_one(
                {"id": draft_id},
                {"$set": {"status": "published", "published_date": datetime.now(timezone.utc).isoformat()}},
            )

        except Exception as e:
            print(f"[VideoPublish {job_id}] failed: {e}")
            await col.update_one(
                {"job_id": job_id},
                {"$set": VideoPublishService._failure_update(e)},
            )

    @staticmethod
    def _failure_update(exc: Exception) -> Dict[str, Any]:
        """Job fields for an exception. A TikTok post that was accepted but hasn't
        reached a final state is "processing" (with the explanation), never
        "published" and not "failed" either — success needs TikTok's confirmation."""
        from app.agents.social_media_manager.services.tiktok_direct_service import (
            TikTokStillProcessingError,
        )
        if isinstance(exc, TikTokStillProcessingError):
            return {"status": "processing", "error": str(exc)}
        return {"status": "failed", "error": str(exc)}

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
        try:
            await col.update_one({"job_id": job_id}, {"$set": {"status": "uploading"}})
            access_token = await get_valid_tiktok_access_token(db, conn)
            publish_id, status = await publish_tiktok_photo_direct(
                access_token, image_urls, tiktok_settings, auto_add_music
            )
            print(f"✅ TikTok direct photo publish: publish_id={publish_id} status={status}")

            await col.update_one(
                {"job_id": job_id},
                {"$set": {"status": "published", "platform_post_id": publish_id, "post_url": None}},
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
            await col.update_one(
                {"job_id": job_id},
                {"$set": VideoPublishService._failure_update(e)},
            )

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
        try:
            await col.update_one({"job_id": job_id}, {"$set": {"status": "uploading"}})
            outstand = OutstandService()
            media_urls = [await outstand.upload_media_from_url(u) for u in image_urls]
            caption = tiktok_settings.description or tiktok_settings.title or ""
            post_id, post_url = await VideoPublishService._publish_tiktok_via_outstand(
                conn["outstand_account_id"], caption, media_urls, tiktok_settings, auto_add_music, photo=True
            )
            await col.update_one(
                {"job_id": job_id},
                {"$set": {"status": "published", "platform_post_id": post_id, "post_url": post_url}},
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
            await col.update_one({"job_id": job_id}, {"$set": VideoPublishService._failure_update(e)})

    # ── Instagram Reels ───────────────────────────────────────────────────────

    @staticmethod
    async def _publish_instagram_reel(
        ig_user_id: str,
        page_access_token: str,
        video_url: str,
        caption: str,
    ):
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: create media container
            create_resp = await client.post(
                f"{GRAPH_BASE}/{ig_user_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "access_token": page_access_token,
                },
            )
            create_data = create_resp.json()
            if "error" in create_data:
                raise ValueError(f"IG container create error: {create_data['error'].get('message')}")
            container_id = create_data["id"]

            # Step 2: poll until FINISHED
            for _ in range(_IG_MAX_POLLS):
                await asyncio.sleep(_IG_POLL_INTERVAL)
                status_resp = await client.get(
                    f"{GRAPH_BASE}/{container_id}",
                    params={"fields": "status_code,status", "access_token": page_access_token},
                )
                status_data = status_resp.json()
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
                raise ValueError(f"IG publish error: {publish_data['error'].get('message')}")

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
                raise ValueError(f"FB /me error: {me_data['error'].get('message')}")
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
                raise ValueError(f"FB video post error: {post_data['error'].get('message')}")

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
        result = await outstand.publish_post(
            outstand_account_ids=[outstand_account_id],
            content=caption,
            media_urls=media_urls,
            platform_config={"tiktok": tiktok_config},
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
            post_data = await outstand.get_post(post_id)
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
