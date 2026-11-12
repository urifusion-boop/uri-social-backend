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
        Render via Orshot API — Studio templates (POST /v1/studio/render).

        Our own templates (PRD Section 4.2: human-authored once per style, in
        Orshot's Studio editor) are Studio templates, NOT Orshot's public
        library templates — those are two genuinely separate endpoints, not
        two names for the same thing:
          - Library templates (Orshot's own pre-built ones, e.g. "website-screenshot"):
            POST /v1/generate/images
          - Studio templates (ours, numeric IDs like 14698):
            POST /v1/studio/render
        Using the library endpoint for a Studio template ID fails with a
        confusingly generic "templateId not found" / "library template not
        found" 400 — confirmed by hand against a real Studio template. Both
        endpoints share the same request/response body shape otherwise
        (templateId/response.{format,type}/modifications → data.content).

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
                    f"{VendorConfig.ORSHOT_API_URL}/studio/render",
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

    # Our internal fields that are image URLs rather than text — everything
    # else in a slot dict is treated as a Placid text layer. Generic across
    # templates since these names are fixed by our own compositor code
    # (brand_compositor_service.py), not by any individual template.
    PLACID_IMAGE_FIELDS = {
        "background_image_url", "logo_url", "product_image",
        "customer_image", "icon_or_image",
    }

    @staticmethod
    def _build_placid_layers(template_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Placid's real payload shape (verified against placid.app/docs/2.0/rest/images,
        not guessed): each layer is a typed object, not a bare value —
        {"title": {"text": "..."}} for text, {"img": {"image": "url"}} for images.
        Translate our vocabulary to this template's real Placid layer names via
        `placid_field_mapping` (same opt-in-only convention as Orshot's
        `field_mapping` — templates whose designer used our exact names need
        no entry at all), then wrap each value by its actual type.
        """
        template_config = get_template_config(template_id)
        field_mapping = template_config.get("placid_field_mapping") or {}

        layers: Dict[str, Any] = {}
        for our_key, value in data.items():
            if not value:
                continue
            placid_key = field_mapping.get(our_key, our_key)
            if our_key in TemplateService.PLACID_IMAGE_FIELDS:
                layers[placid_key] = {"image": value}
            else:
                layers[placid_key] = {"text": value}
        return layers

    @staticmethod
    async def render_via_placid(
        template_id: str,
        data: Dict[str, Any],
        format: str = "1:1"
    ) -> str:
        """
        Render via Placid API — verified against placid.app/docs/2.0/rest/images
        (the earlier version of this function was an unverified guess and was
        wrong on two counts, corrected here):

        1. Layer values are typed objects, not bare strings — see
           _build_placid_layers.
        2. Image creation is ASYNCHRONOUS by default: POST /images returns
           immediately with status="queued", image_url=null, and a
           polling_url — the real image only exists once that polling_url
           reports status="finished". There is no synchronous variant used
           here (Placid's own docs warn create_now=true "may fail under
           load"), so this polls politely instead, same reasoning as why our
           own /v2 endpoints moved to a background-job pattern rather than
           holding a connection open.

        Note: Placid doesn't support carousel in a single call.
        """
        if not VendorConfig.is_placid_available():
            raise TemplateRenderError("Placid not configured")

        template_config = get_template_config(template_id)
        placid_template_id = template_config.get("placid_template_id")

        if not placid_template_id:
            raise TemplateRenderError(f"Template {template_id} not configured in Placid")

        payload = {
            "template_uuid": placid_template_id,
            "layers": TemplateService._build_placid_layers(template_id, data),
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

                if result.get("status") == "finished" and result.get("image_url"):
                    print(f"✅ Placid render completed (sync): {result['image_url']}")
                    return result["image_url"]

                polling_url = result.get("polling_url")
                if not polling_url:
                    raise TemplateRenderError(f"Placid returned no polling_url: {result}")

                # ~30s ceiling at 2s intervals — matches the retry-with-backoff
                # spirit of PRD Section 12 without holding this open forever.
                for _ in range(15):
                    await asyncio.sleep(2)
                    poll_response = await client.get(polling_url, headers=headers)
                    poll_response.raise_for_status()
                    poll_result = poll_response.json()

                    if poll_result.get("status") == "finished" and poll_result.get("image_url"):
                        print(f"✅ Placid render completed (polled): {poll_result['image_url']}")
                        return poll_result["image_url"]
                    if poll_result.get("status") == "error":
                        raise TemplateRenderError(f"Placid render failed: {poll_result}")

                raise TemplateRenderError("Placid render timed out waiting for completion")

        except httpx.HTTPError as e:
            print(f"❌ Placid API error: {e}")
            raise TemplateRenderError(f"Placid API failed: {e}")

    @staticmethod
    def _apply_field_mapping(template_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Translate our abstract slot vocabulary (headline/subhead/promo/cta/...)
        into whatever field names this specific template's designer actually
        used in Orshot Studio. Templates are built independently of our code —
        there's no reason to expect their field names to match ours, and no
        need to require it. A template only needs a `field_mapping` entry in
        template_config.py if its real fields differ from our vocabulary;
        templates whose designer used our exact names need no mapping at all.
        """
        template_config = get_template_config(template_id)
        field_mapping = template_config.get("field_mapping")
        if not field_mapping:
            return data

        static_fields = template_config.get("static_fields", {})
        return {
            **{orshot_key: data.get(our_key, "") for our_key, orshot_key in field_mapping.items()},
            **static_fields,
        }

    @staticmethod
    async def render_with_fallback(
        template_id: str,
        data: Dict[str, Any],
        format: str = "1:1"
    ) -> str:
        """
        Render with automatic fallback.

        PRD Section 12: Try Orshot, fall back to Placid if down — this is the
        default ("auto") behavior and is untouched. VendorConfig.PREFERRED_VENDOR
        is a manual override (VISUAL_ENGINE_PREFERRED_VENDOR env var) for
        A/B-testing template-authoring experience: set to "placid" to force
        every render through Placid only, with no Orshot attempt and no
        fallback either way, so a failure is visible rather than silently
        masked by the other vendor.

        Each vendor gets `data` in our own raw vocabulary, not a shared
        pre-mapped dict — Orshot's and Placid's field name/shape translation
        are unrelated to each other (_apply_field_mapping vs
        _build_placid_layers), so mapping for one is meaningless input to
        the other. Previously this fell back to Placid using Orshot's
        already-mapped dict, which would have sent Placid nonsense field
        names on every real fallback.
        """
        preferred = VendorConfig.get_preferred_vendor()

        if preferred == "placid":
            return await TemplateService.render_via_placid(template_id, data, format)

        if preferred == "orshot":
            mapped_data = TemplateService._apply_field_mapping(template_id, data)
            return await TemplateService.render_via_orshot(template_id, mapped_data, format)

        # "auto": Orshot first, Placid fallback — default, unchanged behavior.
        if VendorConfig.is_orshot_available():
            try:
                mapped_data = TemplateService._apply_field_mapping(template_id, data)
                return await TemplateService.render_via_orshot(template_id, mapped_data, format)
            except TemplateRenderError as e:
                print(f"⚠️ Orshot failed, trying fallback: {e}")

        if VendorConfig.is_placid_available():
            try:
                return await TemplateService.render_via_placid(template_id, data, format)
            except TemplateRenderError as e:
                print(f"❌ Placid fallback also failed: {e}")
                raise

        raise TemplateRenderError("No rendering vendor available")

    # Multi-format rendering (PRD Section 14) happens one level up, in
    # BrandCompositorService.compose_final_render — it parallelizes across
    # formats at the full typesetting-layer level (template selection +
    # render + logo compositing per format), not just the raw render call.

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
