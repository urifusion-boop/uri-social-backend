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
                {"$set": {"status": "failed", "error": str(e)}},
            )

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

        outstand_account_id = conn["outstand_account_id"]
        outstand = OutstandService()

        # TikTok requires media to be fetched from a domain verified in the TikTok
        # developer portal. Route it through Outstand's own media upload first so
        # TikTok pulls from Outstand's (already-verified) domain instead.
        outstand_media_url = await outstand.upload_media_from_url(video_url)

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
        tiktok_config = {"postMode": "DIRECT_POST", "privacyLevel": "PUBLIC_TO_EVERYONE"}
        result = await outstand.publish_post(
            outstand_account_ids=[outstand_account_id],
            content=caption,
            media_urls=[outstand_media_url],
            platform_config={"tiktok": tiktok_config},
        )
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

        if not published and failure_reason and (
            "privacy" in failure_reason.lower() or "unaudited" in failure_reason.lower() or "audit" in failure_reason.lower()
        ):
            print(f"⚠️ TikTok post {post_id} failed on privacyLevel/audit ({failure_reason}) — submitting a new post as SELF_ONLY")
            retry_result = await outstand.publish_post(
                outstand_account_ids=[outstand_account_id],
                content=caption,
                media_urls=[outstand_media_url],
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

        # Live-confirmed 2026-10-16: SELF_ONLY can fail with the exact same
        # "unaudited_client_can_only_post_to_private_accounts" error — the real
        # block is DIRECT_POST itself (likely under TikTok re-review), not the
        # privacy value. Fall back to TikTok's third suggested option,
        # MEDIA_UPLOAD, which drops privacyLevel and delivers to the creator's
        # TikTok inbox as a draft instead of posting directly.
        if not published and failure_reason and (
            "privacy" in failure_reason.lower() or "unaudited" in failure_reason.lower() or "audit" in failure_reason.lower()
        ):
            print(f"⚠️ TikTok post {post_id} still failed on privacyLevel/audit ({failure_reason}) — submitting a new post as MEDIA_UPLOAD")
            media_result = await outstand.publish_post(
                outstand_account_ids=[outstand_account_id],
                content=caption,
                media_urls=[outstand_media_url],
                platform_config={"tiktok": {"postMode": "MEDIA_UPLOAD"}},
            )
            media_post_obj = media_result.get("post") or media_result.get("data") or {}
            if isinstance(media_post_obj, list):
                media_post_obj = media_post_obj[0] if media_post_obj else {}
            media_post_id = media_post_obj.get("id")
            if media_post_id:
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
                raise ValueError(f"TikTok publish failed via Outstand: {failure_reason}")
            raise TimeoutError("TikTok publish via Outstand timed out")

        return post_id, post_url

    @staticmethod
    async def _poll_tiktok_outstand_publish(outstand: "OutstandService", post_id: str, post_url: Optional[str]):
        """Poll Outstand until a TikTok post is confirmed published or fails.

        Returns (published, post_url, failure_reason). See the caller for why
        this can't be caught synchronously around publish_post().
        """
        for _ in range(_OUTSTAND_MAX_POLLS):
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
