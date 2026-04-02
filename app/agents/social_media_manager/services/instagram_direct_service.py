"""
Instagram Graph API direct publishing service.

Used when Instagram is connected via Facebook Page (Instagram API with Facebook Login).
Bypasses Outstand entirely for Instagram — uses the Facebook Page Access Token to
publish to the linked Instagram Business Account.

Flow:
  1. After Facebook OAuth, detect linked Instagram account via page token
  2. Store ig_user_id + page_access_token in social_connections
  3. On publish: create media container → publish container
"""

import httpx
from typing import Any, Dict, Optional

from app.core.config import settings

GRAPH_BASE = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"


class InstagramDirectService:

    @staticmethod
    async def get_instagram_account_from_page(
        page_id: str,
        page_access_token: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Query the Instagram Business Account linked to a Facebook Page.
        Returns the IG account dict or None if not linked.
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{GRAPH_BASE}/{page_id}",
                    params={
                        "fields": "instagram_business_account{id,name,username,profile_picture_url}",
                        "access_token": page_access_token,
                    },
                )
                data = resp.json()
                ig = data.get("instagram_business_account")
                if ig and ig.get("id"):
                    return ig
        except Exception as e:
            print(f"⚠️ Instagram account lookup failed for page {page_id}: {e}")
        return None

    @staticmethod
    async def publish_post(
        ig_user_id: str,
        page_access_token: str,
        content: str,
        image_url: Optional[str] = None,
        scheduled_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish a feed post to Instagram via Graph API.

        Steps:
          1. POST /{ig_user_id}/media   → create media container (returns creation_id)
          2. POST /{ig_user_id}/media_publish → publish the container

        image_url must be a publicly accessible HTTPS URL.
        Instagram requires an image for standard feed posts.
        """
        if not image_url:
            return {
                "success": False,
                "error": "Instagram requires an image for feed posts. Re-generate with 'include_images: true'.",
            }

        # Must be a publicly accessible absolute HTTPS URL
        if not image_url.startswith("https://"):
            return {
                "success": False,
                "error": f"Instagram cannot fetch the image — URL must be a public HTTPS link (got: {image_url[:80]}). Re-generate the post with 'include_images: true'.",
            }

        # Instagram does not support WebP — requires JPEG or PNG
        if image_url.lower().split("?")[0].endswith(".webp"):
            return {
                "success": False,
                "error": "Instagram does not support WebP images. Please re-generate the post without an image, or use a JPEG/PNG image.",
            }

        async with httpx.AsyncClient(timeout=60) as client:
            # Step 1 — create media container
            container_resp = await client.post(
                f"{GRAPH_BASE}/{ig_user_id}/media",
                params={
                    "image_url": image_url,
                    "caption": content,
                    "access_token": page_access_token,
                },
            )
            container_data = container_resp.json()
            creation_id = container_data.get("id")
            if not creation_id:
                error_msg = (container_data.get("error") or {}).get("message", str(container_data))
                print(f"❌ Instagram media container failed: {container_data}")
                return {"success": False, "error": f"Media container error: {error_msg}"}

            # Step 2 — publish
            publish_resp = await client.post(
                f"{GRAPH_BASE}/{ig_user_id}/media_publish",
                params={
                    "creation_id": creation_id,
                    "access_token": page_access_token,
                },
            )
            publish_data = publish_resp.json()
            post_id = publish_data.get("id")
            if post_id:
                print(f"✅ Instagram direct publish success: post_id={post_id}")
            else:
                print(f"❌ Instagram direct publish failed: {publish_data}")
            return {
                "success": bool(post_id),
                "post_id": post_id,
                "raw_response": publish_data,
            }
