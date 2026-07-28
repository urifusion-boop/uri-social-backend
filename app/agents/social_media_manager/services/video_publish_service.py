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
                    conn["outstand_account_id"], video_url, caption
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

    # ── TikTok (via Outstand — no direct TikTok API) ───────────────────────────

    @staticmethod
    async def _publish_tiktok(
        outstand_account_id: str,
        video_url: str,
        caption: str,
    ):
        outstand = OutstandService()

        result = await outstand.publish_post(
            outstand_account_ids=[outstand_account_id],
            content=caption,
            media_urls=[video_url],
        )
        post_obj = result.get("post") or result.get("data") or {}
        if isinstance(post_obj, list):
            post_obj = post_obj[0] if post_obj else {}
        post_id = post_obj.get("id")
        if not post_id:
            raise ValueError(f"Outstand did not return a post id for TikTok publish: {result}")

        # Poll Outstand until the video finishes processing/publishing on TikTok's side.
        post_url = post_obj.get("url") or post_obj.get("permalink")
        for _ in range(_OUTSTAND_MAX_POLLS):
            post_data = await outstand.get_post(post_id)
            post = post_data.get("post") or post_data.get("data") or {}
            if isinstance(post, list):
                post = post[0] if post else {}
            if post.get("status") == "failed":
                raise ValueError(f"TikTok publish failed via Outstand: {post}")
            if post.get("publishedAt") and not post.get("isDraft"):
                post_url = post.get("url") or post.get("permalink") or post_url
                break
            await asyncio.sleep(_OUTSTAND_POLL_INTERVAL)
        else:
            raise TimeoutError("TikTok publish via Outstand timed out")

        return post_id, post_url
