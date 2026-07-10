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
        Render via Orshot API

        Args:
            template_id: Template to use
            data: Slot mappings {slot_name: value}
            format: Aspect ratio

        Returns:
            Rendered image URL (permanently hosted)
        """
        if not VendorConfig.is_orshot_available():
            raise TemplateRenderError("Orshot not configured")

        template_config = get_template_config(template_id)
        orshot_template_id = template_config.get("orshot_template_id")

        if not orshot_template_id:
            raise TemplateRenderError(f"Template {template_id} not configured in Orshot")

        # Build Orshot API payload
        payload = {
            "template_id": orshot_template_id,
            "data": data,
            "format": format,
            "permanent": True,  # PRD requirement: permanent hosting
            "webhook": None  # Synchronous for now
        }

        headers = {
            "Authorization": f"Bearer {VendorConfig.ORSHOT_API_KEY}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{VendorConfig.ORSHOT_API_URL}/render",
                    json=payload,
                    headers=headers
                )
                response.raise_for_status()

                result = response.json()
                render_url = result.get("url") or result.get("image_url")

                if not render_url:
                    raise TemplateRenderError(f"Orshot returned no URL: {result}")

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
        Render via Placid API (fallback vendor)

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
        Render multi-slide carousel

        PRD Section 9: Carousel generation
        Orshot supports native multi-page, Placid doesn't
        """
        if VendorConfig.is_orshot_available():
            # Try Orshot native multi-page (if supported)
            try:
                return await TemplateService._render_carousel_orshot_native(
                    template_id,
                    slides_data,
                    format
                )
            except Exception as e:
                print(f"⚠️ Orshot native carousel failed, rendering slides individually: {e}")

        # Fall back: render each slide individually
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

    @staticmethod
    async def _render_carousel_orshot_native(
        template_id: str,
        slides_data: List[Dict[str, Any]],
        format: str = "1:1"
    ) -> List[str]:
        """Orshot native multi-page carousel (single API call)"""
        template_config = get_template_config(template_id)
        orshot_template_id = template_config.get("orshot_template_id")

        payload = {
            "template_id": orshot_template_id,
            "pages": slides_data,  # Multi-page data
            "format": format,
            "permanent": True
        }

        headers = {
            "Authorization": f"Bearer {VendorConfig.ORSHOT_API_KEY}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=60.0) as client:  # Longer timeout for carousel
            response = await client.post(
                f"{VendorConfig.ORSHOT_API_URL}/render/carousel",
                json=payload,
                headers=headers
            )
            response.raise_for_status()

            result = response.json()
            slide_urls = result.get("pages") or result.get("slides")

            if not slide_urls:
                raise TemplateRenderError(f"Orshot carousel returned no URLs: {result}")

            print(f"✅ Orshot carousel ({len(slide_urls)} slides) completed")
            return slide_urls
