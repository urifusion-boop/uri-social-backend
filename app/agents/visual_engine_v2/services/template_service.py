"""
Template Rendering Service
Orshot (primary) and Placid (fallback) integration
4-layer compositor as per PRD Section 4.1
"""

import httpx
import asyncio
from typing import Dict, Any, Optional, List
from ..config.vendor_config import VendorConfig
from ..config.template_config import get_template_config


class TemplateRenderError(Exception):
    """Template rendering failed"""
    pass


class TemplateService:
    """
    Template-fill rendering layer
    Handles Orshot/Placid API integration
    """

    @staticmethod
    async def render_via_orshot(
        template_id: str,
        data: Dict[str, Any],
        format: str = "1:1"
    ) -> str:
        """
        Render via Orshot API.

        Verified against https://orshot.com/docs/api-reference/render-from-template —
        the real endpoint is POST /v1/generate/images with a templateId/response/
        modifications body; the /v1/render this originally called doesn't exist
        (404, confirmed by hand) and was never actually reachable.

        Args:
            template_id: Our internal template_config.py entry to use
            data: Slot mappings {slot_name: value} — sent as Orshot's "modifications"
            format: Our aspect-ratio concept (1:1/4:5/9:16). Orshot has no render-time
                parameter for this — each aspect ratio is its own fixed-size template,
                already selected via template_config.select_template() before this is
                ever called. Kept as a parameter for logging/API symmetry only.

        Returns:
            Rendered image URL (permanently hosted — response.type="url" is
            confirmed permanent per Orshot's own docs, not an expiring link)
        """
        if not VendorConfig.is_orshot_available():
            raise TemplateRenderError("Orshot not configured")

        template_config = get_template_config(template_id)
        orshot_template_id = template_config.get("orshot_template_id")

        if not orshot_template_id:
            raise TemplateRenderError(f"Template {template_id} not configured in Orshot")

        payload = {
            "templateId": orshot_template_id,
            "response": {
                "format": "png",
                "type": "url"
            },
            "modifications": data
        }

        headers = {
            "Authorization": f"Bearer {VendorConfig.ORSHOT_API_KEY}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{VendorConfig.ORSHOT_API_URL}/generate/images",
                    json=payload,
                    headers=headers
                )
                response.raise_for_status()

                result = response.json()
                render_url = (result.get("data") or {}).get("content")

                if not render_url:
                    raise TemplateRenderError(f"Orshot returned no content: {result}")

                print(f"✅ Orshot render completed: {render_url}")
                return render_url

        except httpx.HTTPError as e:
            print(f"❌ Orshot API error: {e}")
            raise TemplateRenderError(f"Orshot API failed: {e}")

    @staticmethod
    async def render_via_placid(
        template_id: str,
        data: Dict[str, Any],
        format: str = "1:1"
    ) -> str:
        """
        Render via Placid API (fallback vendor).

        Unlike render_via_orshot, this endpoint/payload shape has NOT been
        verified against Placid's real docs (Orshot's was verified by hand and
        was wrong — /v1/render didn't exist). Treat this as equally unverified
        until someone checks it the same way, especially since Placid isn't
        even configured/enabled yet (no PLACID_API_KEY set anywhere).

        Note: Placid doesn't support carousel in single call
        """
        if not VendorConfig.is_placid_available():
            raise TemplateRenderError("Placid not configured")

        template_config = get_template_config(template_id)
        placid_template_id = template_config.get("placid_template_id")

        if not placid_template_id:
            raise TemplateRenderError(f"Template {template_id} not configured in Placid")

        # Build Placid API payload
        payload = {
            "template_uuid": placid_template_id,
            "layers": data,  # Placid uses "layers" instead of "data"
        }

        headers = {
            "Authorization": f"Bearer {VendorConfig.PLACID_API_KEY}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{VendorConfig.PLACID_API_URL}/images",
                    json=payload,
                    headers=headers
                )
                response.raise_for_status()

                result = response.json()
                render_url = result.get("image_url")

                if not render_url:
                    raise TemplateRenderError(f"Placid returned no URL: {result}")

                print(f"✅ Placid render completed: {render_url}")
                return render_url

        except httpx.HTTPError as e:
            print(f"❌ Placid API error: {e}")
            raise TemplateRenderError(f"Placid API failed: {e}")

    @staticmethod
    async def render_with_fallback(
        template_id: str,
        data: Dict[str, Any],
        format: str = "1:1"
    ) -> str:
        """
        Render with automatic fallback

        PRD Section 12: Try Orshot, fall back to Placid if down
        """
        # Try primary vendor (Orshot)
        if VendorConfig.is_orshot_available():
            try:
                return await TemplateService.render_via_orshot(template_id, data, format)
            except TemplateRenderError as e:
                print(f"⚠️ Orshot failed, trying fallback: {e}")

        # Fall back to Placid
        if VendorConfig.is_placid_available():
            try:
                return await TemplateService.render_via_placid(template_id, data, format)
            except TemplateRenderError as e:
                print(f"❌ Placid fallback also failed: {e}")
                raise

        raise TemplateRenderError("No rendering vendor available")

    @staticmethod
    async def render_multi_format(
        template_id: str,
        data: Dict[str, Any],
        formats: List[str] = ["1:1"]
    ) -> Dict[str, str]:
        """
        Render multiple formats (1:1, 4:5, 9:16)

        PRD Section 14: Multi-format output
        Returns: {format: url}
        """
        results = {}

        # Render each format
        tasks = [
            TemplateService.render_with_fallback(template_id, data, fmt)
            for fmt in formats
        ]

        rendered_urls = await asyncio.gather(*tasks, return_exceptions=True)

        for fmt, url in zip(formats, rendered_urls):
            if isinstance(url, Exception):
                print(f"❌ Failed to render format {fmt}: {url}")
                results[fmt] = None
            else:
                results[fmt] = url

        return results

    @staticmethod
    async def render_carousel(
        template_id: str,
        slides_data: List[Dict[str, Any]],
        format: str = "1:1"
    ) -> List[str]:
        """
        Render multi-slide carousel.

        PRD Section 9: Carousel generation. Previously tried an Orshot "native
        multi-page" call first (POST /v1/render/carousel) — that endpoint
        doesn't exist (same class of error as /v1/render; confirmed by hand
        against Orshot's real docs, which don't document any multi-page/
        carousel endpoint). Removed rather than left calling a fictional URL;
        every slide is rendered as its own /v1/generate/images call instead.
        Revisit if Orshot documents real carousel support later.
        """
        tasks = [
            TemplateService.render_with_fallback(template_id, slide_data, format)
            for slide_data in slides_data
        ]

        slide_urls = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out failures
        valid_urls = [
            url for url in slide_urls
            if not isinstance(url, Exception)
        ]

        if not valid_urls:
            raise TemplateRenderError("All carousel slides failed to render")

        return valid_urls
