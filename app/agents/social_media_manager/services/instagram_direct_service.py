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
                        "fields": (
                            "instagram_business_account{id,name,username,profile_picture_url},"
                            "connected_instagram_account{id,name,username,profile_picture_url}"
                        ),
                        "access_token": page_access_token,
                    },
                )
                data = resp.json()
                print(f"[IG Lookup] page_id={page_id} raw_response={data}")

                if "error" in data:
                    err = data["error"]
                    print(f"[IG Lookup] ❌ Graph API error: code={err.get('code')} subcode={err.get('error_subcode')} msg={err.get('message')}")
                    return None

                # Prefer instagram_business_account; fall back to connected_instagram_account
                ig = data.get("instagram_business_account") or data.get("connected_instagram_account")
                if ig and ig.get("id"):
                    source = "instagram_business_account" if data.get("instagram_business_account") else "connected_instagram_account"
                    print(f"[IG Lookup] ✅ Found Instagram account via {source}: id={ig.get('id')} username=@{ig.get('username')}")
                    return ig

                print(
                    f"[IG Lookup] ℹ️ Neither instagram_business_account nor connected_instagram_account returned. "
                    f"Token may lack instagram_basic permission, or the Instagram account is not linked to this page."
                )
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

            # Step 1b — wait for container to finish processing (status_code = FINISHED)
            # Instagram processes the image asynchronously; publishing before FINISHED
            # causes error_subcode 2207027 ("Media is not ready to be published").
            import asyncio as _asyncio
            for attempt in range(12):  # poll up to ~60s (12 × 5s)
                status_resp = await client.get(
                    f"{GRAPH_BASE}/{creation_id}",
                    params={"fields": "status_code", "access_token": page_access_token},
                )
                status_code = status_resp.json().get("status_code", "")
                print(f"⏳ Container {creation_id} status: {status_code} (attempt {attempt + 1})")
                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    return {"success": False, "error": "Instagram container processing failed (status=ERROR)."}
                await _asyncio.sleep(5)
            else:
                return {"success": False, "error": "Instagram container timed out waiting for FINISHED status."}

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

    @staticmethod
    async def publish_carousel(
        ig_user_id: str,
        page_access_token: str,
        caption: str,
        slides: list,
        page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish a carousel post to Instagram.

        slides: list of dicts with at least "image_url" key.

        Steps:
          1. Re-host each slide image to Facebook CDN.
          2. Create carousel item containers (sequentially — Instagram requirement).
          3. Create carousel container with media_type=CAROUSEL.
          4. Poll carousel container until status_code == FINISHED.
          5. Publish via /media_publish.
        """
        import asyncio as _asyncio

        item_ids = []
        async with httpx.AsyncClient(timeout=60) as client:
            for i, slide in enumerate(slides):
                image_url = slide.get("image_url") or ""
                if not image_url:
                    print(f"⚠️ Carousel slide {i} has no image_url — skipping")
                    continue

                # Re-host to Facebook CDN so Instagram can always fetch it
                jpeg_bytes = await InstagramDirectService._download_as_jpeg(image_url)
                if not jpeg_bytes:
                    print(f"⚠️ Could not download slide {i} image — skipping")
                    continue
                if page_id:
                    cdn_url = await InstagramDirectService._upload_to_facebook_cdn(
                        page_id, page_access_token, jpeg_bytes
                    )
                else:
                    cdn_url = image_url  # best-effort fallback
                if not cdn_url:
                    print(f"⚠️ CDN upload failed for slide {i} — skipping")
                    continue

                # Create carousel item container
                item_resp = await client.post(
                    f"{GRAPH_BASE}/{ig_user_id}/media",
                    params={
                        "image_url": cdn_url,
                        "is_carousel_item": "true",
                        "access_token": page_access_token,
                    },
                )
                item_data = item_resp.json()
                item_id = item_data.get("id")
                if not item_id:
                    error_msg = (item_data.get("error") or {}).get("message", str(item_data))
                    print(f"❌ Carousel item {i} container failed: {item_data}")
                    return {"success": False, "error": f"Carousel item {i} container error: {error_msg}"}
                item_ids.append(item_id)
                print(f"✅ Carousel item {i} container created: {item_id}")

            if not item_ids:
                return {"success": False, "error": "No carousel items could be created (all slides missing images)."}

            # Create carousel container
            carousel_resp = await client.post(
                f"{GRAPH_BASE}/{ig_user_id}/media",
                params={
                    "media_type": "CAROUSEL",
                    "children": ",".join(item_ids),
                    "caption": caption,
                    "access_token": page_access_token,
                },
            )
            carousel_data = carousel_resp.json()
            creation_id = carousel_data.get("id")
            if not creation_id:
                error_msg = (carousel_data.get("error") or {}).get("message", str(carousel_data))
                print(f"❌ Carousel container failed: {carousel_data}")
                return {"success": False, "error": f"Carousel container error: {error_msg}"}

            # Poll until FINISHED
            for attempt in range(12):
                status_resp = await client.get(
                    f"{GRAPH_BASE}/{creation_id}",
                    params={"fields": "status_code", "access_token": page_access_token},
                )
                status_code = status_resp.json().get("status_code", "")
                print(f"⏳ Carousel container {creation_id} status: {status_code} (attempt {attempt + 1})")
                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    return {"success": False, "error": "Instagram carousel container processing failed (status=ERROR)."}
                await _asyncio.sleep(5)
            else:
                return {"success": False, "error": "Instagram carousel container timed out waiting for FINISHED status."}

            # Publish
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
                print(f"✅ Instagram carousel publish success: post_id={post_id}")
            else:
                print(f"❌ Instagram carousel publish failed: {publish_data}")
            return {
                "success": bool(post_id),
                "post_id": post_id,
                "raw_response": publish_data,
            }

    @staticmethod
    async def publish_story(
        ig_user_id: str,
        page_access_token: str,
        image_url: Optional[str] = None,
        page_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish a story to Instagram.

        Steps:
          1. Re-host image to Facebook CDN.
          2. POST /{ig_user_id}/media with media_type=STORIES.
          3. Poll until FINISHED.
          4. POST /{ig_user_id}/media_publish.
        """
        import asyncio as _asyncio

        if not image_url:
            return {"success": False, "error": "Instagram Stories require an image."}

        # Re-host to Facebook CDN
        jpeg_bytes = await InstagramDirectService._download_as_jpeg(image_url)
        if not jpeg_bytes:
            return {"success": False, "error": "Could not download/convert story image."}
        if page_id:
            cdn_url = await InstagramDirectService._upload_to_facebook_cdn(
                page_id, page_access_token, jpeg_bytes
            )
        else:
            cdn_url = image_url
        if not cdn_url:
            return {"success": False, "error": "Could not host story image for Instagram publishing."}

        async with httpx.AsyncClient(timeout=60) as client:
            # Create story container
            container_resp = await client.post(
                f"{GRAPH_BASE}/{ig_user_id}/media",
                params={
                    "image_url": cdn_url,
                    "media_type": "STORIES",
                    "access_token": page_access_token,
                },
            )
            container_data = container_resp.json()
            creation_id = container_data.get("id")
            if not creation_id:
                error_msg = (container_data.get("error") or {}).get("message", str(container_data))
                print(f"❌ Instagram story container failed: {container_data}")
                return {"success": False, "error": f"Story container error: {error_msg}"}

            # Poll until FINISHED
            for attempt in range(12):
                status_resp = await client.get(
                    f"{GRAPH_BASE}/{creation_id}",
                    params={"fields": "status_code", "access_token": page_access_token},
                )
                status_code = status_resp.json().get("status_code", "")
                print(f"⏳ Story container {creation_id} status: {status_code} (attempt {attempt + 1})")
                if status_code == "FINISHED":
                    break
                if status_code == "ERROR":
                    return {"success": False, "error": "Instagram story container processing failed (status=ERROR)."}
                await _asyncio.sleep(5)
            else:
                return {"success": False, "error": "Instagram story container timed out waiting for FINISHED status."}

            # Publish
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
                print(f"✅ Instagram story publish success: post_id={post_id}")
            else:
                print(f"❌ Instagram story publish failed: {publish_data}")
            return {
                "success": bool(post_id),
                "post_id": post_id,
                "raw_response": publish_data,
            }
