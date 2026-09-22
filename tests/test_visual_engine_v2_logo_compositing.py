"""
Visual Engine V2 — logo is always self-composited, regardless of
logo_control_mode.

Live-confirmed bug: a brand's logo_size (small/medium/large) had no effect
on the rendered logo whenever logo_control_mode was "agent" (the default
every brand starts on, and the only mode most users ever see since the
toggle for it lives in a separate settings panel from where logo_size is
set). In "agent" mode, the template vendor (Orshot/Placid) was asked to
place the logo natively — but neither vendor has a generic "resize this
image layer by X%" mechanism via arbitrary modification/layer keys, so the
logo rendered at whatever fixed size that specific template's own logo
slot happened to be designed at, completely ignoring logo_size.

Fix: the app now always self-composites the logo via
ImageContentService._overlay_logo (which DOES correctly read logo_size),
never handing logo_url/logo_position to the template vendor at all — so
this is the only path exercised now, for every brand, every render.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.visual_engine_v2.models.visual_engine_models import LayerData
from app.agents.visual_engine_v2.services.brand_compositor_service import BrandCompositorService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _content_layer():
    return LayerData(
        layer_type="content",
        data={"headline": "Big Sale", "subtext": "This week only", "promo": "", "cta": "Shop now"},
        metadata={"post_intent": "promotion"},
    )


def _imagery_layer():
    return LayerData(
        layer_type="imagery",
        data={"imagery_url": "https://example.com/bg.png"},
        metadata={"path": "both"},
    )


def _brand_layer(logo_control_mode: str, logo_size: str = "large"):
    return LayerData(
        layer_type="brand",
        data={
            "brand_name": "SunPay Solar",
            "logo_url": "https://example.com/logo.png",
            "logo_position": "bottom_right",
            "logo_size": logo_size,
            "primary_color": "#111111",
            "secondary_color": "#FFFFFF",
            "accent_color": "#FF5722",
            "primary_font": "Inter",
            "secondary_font": "Inter",
            "style_family": "modern_professional",
            "logo_control_mode": logo_control_mode,
            "logo_manual_position": None,
        },
        metadata={"source": "database"},
    )


def _service_with_mocks():
    service = BrandCompositorService(db=MagicMock())
    service.template_service.render_with_fallback = AsyncMock(return_value="https://cdn.example.com/rendered.png")
    service._composite_user_logo = AsyncMock(return_value="https://cdn.example.com/rendered-with-logo.png")
    return service


@patch("app.agents.visual_engine_v2.services.brand_compositor_service.select_template", return_value="tmpl_1")
def test_logo_composited_even_in_agent_mode(mock_select):
    """The exact bug: logo_control_mode='agent' (the default) must not skip
    self-compositing — that was the whole reason logo_size did nothing."""
    service = _service_with_mocks()
    result = _run(service._render_typesetting_layer(
        content_layer=_content_layer(),
        brand_layer=_brand_layer(logo_control_mode="agent", logo_size="large"),
        format="1:1",
        carousel_count=1,
        imagery_layer=_imagery_layer(),
    ))

    service._composite_user_logo.assert_awaited_once()
    call_args = service._composite_user_logo.await_args.args
    assert call_args[1] == "https://example.com/logo.png"  # logo_url
    assert call_args[3] == "large"  # logo_size actually reached the compositor
    assert result.data["rendered_urls"] == ["https://cdn.example.com/rendered-with-logo.png"]


@patch("app.agents.visual_engine_v2.services.brand_compositor_service.select_template", return_value="tmpl_1")
def test_logo_composited_in_user_mode_too(mock_select):
    """Not a regression for the mode that already worked before this fix."""
    service = _service_with_mocks()
    _run(service._render_typesetting_layer(
        content_layer=_content_layer(),
        brand_layer=_brand_layer(logo_control_mode="user", logo_size="medium"),
        format="1:1",
        carousel_count=1,
        imagery_layer=_imagery_layer(),
    ))
    service._composite_user_logo.assert_awaited_once()
    assert service._composite_user_logo.await_args.args[3] == "medium"


@patch("app.agents.visual_engine_v2.services.brand_compositor_service.select_template", return_value="tmpl_1")
def test_template_vendor_never_receives_logo_url_or_position(mock_select):
    """The vendor placing its own logo (ignoring logo_size) is exactly the
    behavior being removed — it must never be sent a populated logo_url."""
    service = _service_with_mocks()
    _run(service._render_typesetting_layer(
        content_layer=_content_layer(),
        brand_layer=_brand_layer(logo_control_mode="agent", logo_size="large"),
        format="1:1",
        carousel_count=1,
        imagery_layer=_imagery_layer(),
    ))
    sent_data = service.template_service.render_with_fallback.await_args.kwargs["data"]
    assert sent_data["logo_url"] == ""
    assert sent_data["logo_position"] == ""


@patch("app.agents.visual_engine_v2.services.brand_compositor_service.select_template", return_value="tmpl_1")
def test_no_logo_url_skips_compositing(mock_select):
    service = _service_with_mocks()
    brand_layer = _brand_layer(logo_control_mode="agent")
    brand_layer.data["logo_url"] = None
    _run(service._render_typesetting_layer(
        content_layer=_content_layer(),
        brand_layer=brand_layer,
        format="1:1",
        carousel_count=1,
        imagery_layer=_imagery_layer(),
    ))
    service._composite_user_logo.assert_not_awaited()


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
