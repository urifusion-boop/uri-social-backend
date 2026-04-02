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

import base64
import io

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
    async def _download_as_jpeg(url: str) -> Optional[bytes]:
        """Download any image URL and return JPEG bytes (converts WebP/PNG if needed)."""
        from PIL import Image
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            return buf.getvalue()
        except Exception as e:
            print(f"⚠️ Image download/convert failed: {e}")
            return None

    @staticmethod
    async def _upload_to_facebook_cdn(
        page_id: str,
        page_access_token: str,
        jpeg_bytes: bytes,
    ) -> Optional[str]:
        """
        Upload JPEG bytes as an unpublished Facebook photo and return a Facebook CDN URL.
        Instagram's media servers can always access Facebook CDN — this avoids imgBB
        accessibility issues where Meta's crawler is blocked by third-party image hosts.
        """
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                # Upload as unpublished photo to get a Facebook-hosted URL
                upload_resp = await client.post(
                    f"{GRAPH_BASE}/{page_id}/photos",
                    data={"access_token": page_access_token, "published": "false"},
                    files={"source": ("image.jpg", jpeg_bytes, "image/jpeg")},
                )
                upload_data = upload_resp.json()
                photo_id = upload_data.get("id")
                if not photo_id:
                    print(f"⚠️ Facebook CDN upload failed: {upload_data}")
                    return None

                # Retrieve the highest-resolution CDN URL
                meta_resp = await client.get(
                    f"{GRAPH_BASE}/{photo_id}",
                    params={"fields": "images", "access_token": page_access_token},
                )
                images = meta_resp.json().get("images", [])
                if not images:
                    return None
                largest = max(images, key=lambda x: x.get("width", 0) * x.get("height", 0))
                cdn_url = largest.get("source")
                print(f"☁️  Facebook CDN URL for Instagram: {cdn_url}")
                return cdn_url
        except Exception as e:
            print(f"⚠️ Facebook CDN upload error: {e}")
            return None

    @staticmethod
    async def publish_post(
        ig_user_id: str,
        page_access_token: str,
        content: str,
        image_url: Optional[str] = None,
        scheduled_at: Optional[str] = None,
        page_id: Optional[str] = None,
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

        # Instagram requires a publicly accessible JPEG/PNG URL that Meta's servers can fetch.
        # imgBB and similar free hosts are often blocked by Meta's CDN crawler, so we:
        # 1. Download the image and convert to JPEG in memory
        # 2. Upload to Facebook CDN (using the page token) — guaranteed accessible from Instagram
        # 3. Fall back to imgBB only if no page_id is available
        needs_rehost = (
            image_url.lower().split("?")[0].endswith(".webp")
            or "ibb.co" in image_url
        )
        if needs_rehost:
            print(f"🔄 Re-hosting image for Instagram (source: {image_url[:80]})")
            jpeg_bytes = await InstagramDirectService._download_as_jpeg(image_url)
            if not jpeg_bytes:
                return {
                    "success": False,
                    "error": "Could not download/convert image for Instagram. Please re-generate the post.",
                }
            if page_id:
                rehosted = await InstagramDirectService._upload_to_facebook_cdn(
                    page_id, page_access_token, jpeg_bytes
                )
            else:
                # Fallback: imgBB (may not always work with Instagram)
                b64 = base64.b64encode(jpeg_bytes).decode()
                api_key = getattr(settings, "IMGBB_API_KEY", None)
                rehosted = None
                if api_key:
                    async with httpx.AsyncClient(timeout=30) as _c:
                        r = await _c.post(
                            "https://api.imgbb.com/1/upload",
                            data={"key": api_key, "image": b64},
                        )
                        rehosted = r.json().get("data", {}).get("url")
            if not rehosted:
                return {
                    "success": False,
                    "error": "Could not host image for Instagram publishing.",
                }
            print(f"✅ Re-hosted image URL: {rehosted}")
            image_url = rehosted

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
