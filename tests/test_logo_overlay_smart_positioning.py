"""
Logo/headline overlap bug — live-reported by a user on the "Eurolinks Decor"
poster: the brand's small logo visually collided with the AI-generated
headline text, and editing an existing design produced a NEW logo
overlapping the OLD one instead of cleanly replacing it.

Root causes, confirmed by reading every _overlay_logo call site:

1. Every AI-generated-image path except one (complete_social_manager.py's
   manual-upload flow) pasted the logo at a hardcoded corner (brand_context's
   configured logo_position, default "bottom_right") with zero awareness of
   what the model actually rendered there — so a headline placed in that
   corner collides with the logo every time. rank_overlay_positions_cv()
   already existed to rank corners by actual pixel busyness, but only that
   one call site used it.

2. image_editing_service._call_edit_api() sent the ALREADY-logo-composited
   image straight to openai images.edit() with no mask — the old logo's
   pixels were fair game for the edit model to redraw/duplicate, and the
   deterministic re-paste that ran afterward simply pasted the fresh logo on
   top of whatever the edit model left behind, not onto a clean image.

Fixes:
- overlay_logo_smart(): CV-busyness-ranked position selection (reusing
  rank_overlay_positions_cv + the existing AI conflict check) wired into
  every AI-generated-image call site.
- clear_logo_region(): inpaints away the old logo's badge rectangle on the
  edit's INPUT image before it ever reaches images.edit(), using the exact
  same badge geometry _overlay_logo uses to paste, so nothing is left for
  the edit model to preserve or distort.
"""
import asyncio
import base64
import io

from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from app.agents.social_media_manager.services.image_content_service import ImageContentService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _png_bytes(w, h, color):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# ── _compute_logo_badge_geometry — refactor must not change placement math ──

def test_badge_geometry_bottom_right_matches_hand_computed_formula():
    bw, bh = 1000, 1000
    bx, by, badge_w, badge_h, target_w, target_h = ImageContentService._compute_logo_badge_geometry(
        bw, bh, logo_native_size=(200, 100), logo_size_pct=0.08, position="bottom_right"
    )
    assert target_w == 80  # 1000 * 0.08
    assert target_h == 40  # preserves 2:1 aspect ratio
    edge_pad = max(20, int(bw * 0.03))
    badge_pad_inner = max(5, int(bw * 0.005))
    assert badge_w == target_w + badge_pad_inner * 2
    assert badge_h == target_h + badge_pad_inner * 2
    assert bx == bw - badge_w - edge_pad
    assert by == bh - badge_h - edge_pad


def test_badge_geometry_top_left_matches_hand_computed_formula():
    bw, bh = 1000, 1000
    bx, by, badge_w, badge_h, _, _ = ImageContentService._compute_logo_badge_geometry(
        bw, bh, logo_native_size=(200, 100), logo_size_pct=0.08, position="top_left"
    )
    edge_pad = max(20, int(bw * 0.03))
    assert bx == edge_pad
    assert by == edge_pad


# ── overlay_logo_smart — busyness ranking + conflict-check retry ────────────

def _b64_of(color):
    return base64.b64encode(_png_bytes(50, 50, color)).decode()


def test_overlay_logo_smart_accepts_preferred_position_when_clean():
    """Explicit brand_context.logo_position wins whenever it isn't the
    source of a problem — no reason to second-guess a fine placement."""
    src_b64 = _b64_of((255, 255, 255, 255))

    with patch.object(
        ImageContentService, "rank_overlay_positions_cv",
        return_value=["top_left", "bottom_right", "top_right"],
    ) as mock_rank, \
        patch.object(ImageContentService, "_overlay_logo", side_effect=lambda b64, url, pos, size, return_geometry=False: (f"result@{pos}", {"x": 0, "y": 0, "width": 10, "height": 10})) as mock_overlay, \
        patch.object(ImageContentService, "composited_overlay_has_conflict", new=AsyncMock(return_value=False)) as mock_conflict:

        result = _run(ImageContentService.overlay_logo_smart(
            src_b64, "https://example.com/logo.png", "small", preferred_position="bottom_right"
        ))

    assert result == "result@bottom_right"
    mock_overlay.assert_called_once()
    assert mock_overlay.call_args.args[2] == "bottom_right"
    mock_conflict.assert_awaited_once()


def test_overlay_logo_smart_falls_back_when_preferred_position_conflicts():
    """The actual live bug's shape: the configured corner collides with the
    headline, so the next-ranked (actually empty) corner must be used
    instead of forcing the collision through."""
    src_b64 = _b64_of((255, 255, 255, 255))

    conflicts = {"bottom_right": True, "top_left": False}

    async def fake_conflict(data_url, position):
        return conflicts[position]

    with patch.object(
        ImageContentService, "rank_overlay_positions_cv",
        return_value=["top_left", "top_right", "bottom_left"],
    ), \
        patch.object(ImageContentService, "_overlay_logo", side_effect=lambda b64, url, pos, size, return_geometry=False: (f"result@{pos}", {"x": 0, "y": 0, "width": 10, "height": 10})) as mock_overlay, \
        patch.object(ImageContentService, "composited_overlay_has_conflict", new=AsyncMock(side_effect=fake_conflict)):

        result = _run(ImageContentService.overlay_logo_smart(
            src_b64, "https://example.com/logo.png", "small", preferred_position="bottom_right"
        ))

    assert result == "result@top_left"
    attempted_positions = [c.args[2] for c in mock_overlay.call_args_list]
    assert attempted_positions == ["bottom_right", "top_left"]


def test_overlay_logo_smart_uses_pure_cv_ranking_without_a_preferred_position():
    src_b64 = _b64_of((255, 255, 255, 255))

    with patch.object(
        ImageContentService, "rank_overlay_positions_cv",
        return_value=["top_center", "bottom_center", "center"],
    ), \
        patch.object(ImageContentService, "_overlay_logo", side_effect=lambda b64, url, pos, size, return_geometry=False: (f"result@{pos}", {"x": 0, "y": 0, "width": 10, "height": 10})) as mock_overlay, \
        patch.object(ImageContentService, "composited_overlay_has_conflict", new=AsyncMock(return_value=False)):

        result = _run(ImageContentService.overlay_logo_smart(
            src_b64, "https://example.com/logo.png", "small", preferred_position=None
        ))

    assert result == "result@top_center"
    assert mock_overlay.call_args.args[2] == "top_center"


def test_overlay_logo_smart_keeps_last_attempt_when_all_three_candidates_conflict():
    src_b64 = _b64_of((255, 255, 255, 255))

    with patch.object(
        ImageContentService, "rank_overlay_positions_cv",
        return_value=["top_left", "top_right", "bottom_left", "bottom_right"],
    ), \
        patch.object(ImageContentService, "_overlay_logo", side_effect=lambda b64, url, pos, size, return_geometry=False: (f"result@{pos}", {"x": 0, "y": 0, "width": 10, "height": 10})) as mock_overlay, \
        patch.object(ImageContentService, "composited_overlay_has_conflict", new=AsyncMock(return_value=True)):

        result = _run(ImageContentService.overlay_logo_smart(
            src_b64, "https://example.com/logo.png", "small", preferred_position=None
        ))

    # Only the top 3 candidates are ever tried, and the last one is kept
    # rather than leaving the image with no logo at all.
    assert result == "result@bottom_left"
    assert mock_overlay.call_count == 3


# ── clear_logo_region — inpaint the old logo out before an AI edit call ─────

def test_clear_logo_region_erases_exactly_the_badge_rect_overlay_logo_would_use():
    """Paint a fake 'old logo' at exactly the rect _compute_logo_badge_geometry
    reports for these params, then confirm clear_logo_region removes it
    (the previously-magenta center blends back toward the white background)
    instead of leaving it for an edit model to redraw/duplicate."""
    bw, bh = 300, 300
    base_img = Image.new("RGB", (bw, bh), (255, 255, 255))

    logo_size_pct = 0.08
    bx, by, badge_w, badge_h, _, _ = ImageContentService._compute_logo_badge_geometry(
        bw, bh, logo_native_size=(80, 40), logo_size_pct=logo_size_pct, position="bottom_right"
    )
    # Paint the "old logo" region solid magenta — stands in for whatever
    # pixels a prior _overlay_logo() call actually baked in there.
    for x in range(bx, bx + badge_w):
        for y in range(by, by + badge_h):
            base_img.putpixel((x, y), (255, 0, 255))

    fake_logo_bytes = _png_bytes(80, 40, (0, 0, 0, 255))
    fake_response = MagicMock()
    fake_response.content = fake_logo_bytes
    fake_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=fake_response):
        cleared = ImageContentService.clear_logo_region(
            base_img, "https://example.com/logo.png", position="bottom_right", logo_size="small"
        )

    assert cleared.size == (bw, bh)
    center_pixel = cleared.convert("RGB").getpixel((bx + badge_w // 2, by + badge_h // 2))
    # Inpainting blends toward the surrounding white — the old magenta must
    # not survive untouched into the image sent to the edit model.
    assert center_pixel != (255, 0, 255)


def test_clear_logo_region_falls_back_to_original_image_on_download_failure():
    base_img = Image.new("RGB", (100, 100), (10, 20, 30))
    with patch("requests.get", side_effect=Exception("network down")):
        result = ImageContentService.clear_logo_region(
            base_img, "https://example.com/logo.png", position="bottom_right", logo_size="small"
        )
    assert result is base_img


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
