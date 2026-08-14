"""
Background Removal Utility
Removes backgrounds from product images to create clean cutouts.

Supports two methods:
1. remove.bg API (paid, best quality) - Recommended for production
2. rembg library (free, good quality) - Fallback option

PRD: URI-Social-Product-Preservation-Pipeline.docx - Step 2
"""

import base64
import os
import io
import re
import asyncio
from typing import Optional
import httpx


async def _load_image_bytes(image_url: str) -> bytes:
    """Load raw image bytes from either a data: URI or a real hosted URL.

    Every method below used to assume image_url was always a fetchable HTTP(S)
    URL and crashed with "No connection adapters were found for 'data:image/...'"
    whenever a data: URI was passed in instead (routine for browser-uploaded
    images, which are frequently base64-encoded client-side rather than hosted
    first) — confirmed live via the DALL-E-2 fallback path. Mirrors the
    dual-path handling already used correctly in image_content_service.py's
    _call_dalle_api for the same reference_image parameter."""
    if image_url.startswith("data:"):
        match = re.match(r"data:[^;]+;base64,(.+)", image_url, re.DOTALL)
        if not match:
            raise ValueError("Malformed data: URI — expected data:<mime>;base64,<payload>")
        return base64.b64decode(match.group(1))

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        return resp.content


async def remove_background_removebg(image_url: str) -> Optional[bytes]:
    """
    Remove background using remove.bg API.

    Cost: ~$0.01-0.05 per image
    Quality: Excellent (best option)

    Args:
        image_url: URL of the product image

    Returns:
        PNG bytes with transparent background, or None if failed
    """
    api_key = os.environ.get("REMOVEBG_API_KEY")

    if not api_key:
        print("⚠️ REMOVEBG_API_KEY not set, skipping remove.bg")
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if image_url.startswith("data:"):
                # remove.bg's image_url param expects a fetchable URL — it can't
                # resolve a data: URI either, so send the raw bytes as a file
                # upload instead (its documented alternative input mode).
                image_bytes = await _load_image_bytes(image_url)
                response = await client.post(
                    "https://api.remove.bg/v1.0/removebg",
                    files={"image_file": image_bytes},
                    data={"size": "auto"},
                    headers={"X-Api-Key": api_key},
                )
            else:
                response = await client.post(
                    "https://api.remove.bg/v1.0/removebg",
                    data={"image_url": image_url, "size": "auto"},
                    headers={"X-Api-Key": api_key},
                )

            if response.status_code == 200:
                print(f"✂️ Background removed via remove.bg API")
                return response.content
            else:
                print(f"⚠️ remove.bg API error {response.status_code}: {response.text}")
                return None

    except Exception as e:
        print(f"⚠️ remove.bg error: {str(e)}")
        return None


async def remove_background_rembg(image_url: str) -> Optional[bytes]:
    """
    Remove background using rembg library (local processing).

    Cost: $0 (runs locally)
    Quality: Good (slightly lower than remove.bg)

    Args:
        image_url: URL of the product image

    Returns:
        PNG bytes with transparent background, or None if failed
    """
    try:
        # Check if rembg is installed
        try:
            from rembg import remove
            from PIL import Image
        except ImportError:
            print("⚠️ rembg not installed. Install with: pip install rembg[gpu] or pip install rembg")
            return None

        # Load the image — data: URI or real URL, see _load_image_bytes
        image_bytes = await _load_image_bytes(image_url)
        input_image = Image.open(io.BytesIO(image_bytes))

        # Remove background (CPU-intensive, run in executor)
        def _remove_bg():
            return remove(input_image)

        output_image = await asyncio.get_event_loop().run_in_executor(None, _remove_bg)

        # Convert to PNG bytes
        output_buffer = io.BytesIO()
        output_image.save(output_buffer, format="PNG")
        output_buffer.seek(0)

        print(f"✂️ Background removed via rembg (local)")
        return output_buffer.read()

    except Exception as e:
        print(f"⚠️ rembg error: {str(e)}")
        return None


async def remove_background_dalle(image_url: str) -> Optional[str]:
    """
    Remove background using DALL-E-2 edit mode (fallback).

    Cost: ~$0.02 per image
    Quality: Moderate (may modify product slightly)

    Args:
        image_url: URL of the product image

    Returns:
        Cloudinary URL of cutout, or None if failed
    """
    try:
        from app.services.AIService import client as openai_client

        # Load the image — data: URI or real URL, see _load_image_bytes. This is
        # the exact call that used to crash with "No connection adapters were
        # found for 'data:image/...'" whenever reference_image was a data: URI
        # (routine for browser uploads) rather than a hosted URL.
        image_bytes = io.BytesIO(await _load_image_bytes(image_url))

        # Use DALL-E-2 edit mode
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: openai_client.images.edit(
                model="dall-e-2",
                image=image_bytes,
                prompt="Remove the background completely. Keep only the product on a transparent background. Do not modify the product in any way — preserve exact colours, details, and proportions.",
                n=1,
                size="1024x1024"
            )
        )

        cutout_url = result.data[0].url
        print(f"✂️ Background removed via DALL-E-2 edit mode")

        return cutout_url

    except Exception as e:
        print(f"⚠️ DALL-E-2 background removal error: {str(e)}")
        return None


async def remove_background(image_url: str, method: str = "auto") -> Optional[str]:
    """
    Remove background from product image (multi-method with fallback).

    PRD: Step 2 - Background Removal

    Tries methods in order:
    1. remove.bg API (if API key configured)
    2. rembg library (if installed)
    3. DALL-E-2 edit mode (fallback)
    4. Return original image (if all fail)

    Args:
        image_url: URL of the product image
        method: "auto" (try all), "removebg", "rembg", "dalle"

    Returns:
        Cloudinary URL of the cutout with transparent background
    """
    from app.utils.cloudinary_upload import upload_bytes

    cutout_bytes = None
    cutout_url = None

    # Try methods based on preference
    if method in ("auto", "removebg"):
        cutout_bytes = await remove_background_removebg(image_url)
        if cutout_bytes:
            cutout_url = await upload_bytes(
                cutout_bytes,
                folder="uri-social/product-cutouts",
                resource_type="image"
            )
            print(f"☁️ Cutout uploaded to Cloudinary: {cutout_url}")
            return cutout_url

    if method in ("auto", "rembg") and not cutout_url:
        cutout_bytes = await remove_background_rembg(image_url)
        if cutout_bytes:
            cutout_url = await upload_bytes(
                cutout_bytes,
                folder="uri-social/product-cutouts",
                resource_type="image"
            )
            print(f"☁️ Cutout uploaded to Cloudinary: {cutout_url}")
            return cutout_url

    if method in ("auto", "dalle") and not cutout_url:
        cutout_url = await remove_background_dalle(image_url)
        if cutout_url:
            # DALL-E-2 returns a URL, need to download and re-upload to Cloudinary
            try:
                import requests
                response = requests.get(cutout_url, timeout=10)
                response.raise_for_status()
                cutout_url = await upload_bytes(
                    response.content,
                    folder="uri-social/product-cutouts",
                    resource_type="image"
                )
                print(f"☁️ Cutout uploaded to Cloudinary: {cutout_url}")
                return cutout_url
            except Exception as e:
                print(f"⚠️ Failed to re-upload DALL-E cutout: {e}")

    # If all methods fail, return original image
    print(f"⚠️ All background removal methods failed, using original image")
    return image_url
