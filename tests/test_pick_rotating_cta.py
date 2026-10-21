"""
_pick_rotating_cta — the one CTA-selection function every content-generation
path shares (app/agents/social_media_manager/routers/complete_social_manager.py).

Confirmed live (Oct 2026): four call sites (the carousel shared-context
resolver, _generate_image_bg's carousel and regular-post branches, the
media-upload CTA overlay, and the video-outro CTA) each independently
reimplemented this. Three agreed on deterministic round-robin via
cta_rotation_index; one used random.choice, so the same brand saw a
different CTA rotation behaviour depending on which path generated the
post. A fifth (video outro) always took cta_styles[0], so a brand with
several CTA styles configured never saw any but the first one there. One
pure function now, so none of them can drift again.
"""
from app.agents.social_media_manager.routers.complete_social_manager import _pick_rotating_cta


class TestRotation:
    def test_picks_the_style_at_the_given_index(self):
        cta, nxt = _pick_rotating_cta(["DM us", "Call now", "Book today"], 1)
        assert cta == "Call now"

    def test_advances_to_the_next_index(self):
        _, nxt = _pick_rotating_cta(["DM us", "Call now", "Book today"], 1)
        assert nxt == 2

    def test_wraps_around_at_the_end_of_the_list(self):
        cta, nxt = _pick_rotating_cta(["DM us", "Call now"], 1)
        assert cta == "Call now" and nxt == 0

    def test_an_out_of_range_index_resets_to_the_start(self):
        cta, nxt = _pick_rotating_cta(["DM us", "Call now"], 99)
        assert cta == "DM us" and nxt == 1

    def test_is_deterministic_not_random(self):
        # Same input, same output, every time — the old random.choice path
        # could return a different CTA on every call for the same index.
        results = {_pick_rotating_cta(["a", "b", "c"], 0)[0] for _ in range(20)}
        assert results == {"a"}


class TestFallback:
    def test_empty_cta_styles_falls_back_to_default_link(self):
        cta, nxt = _pick_rotating_cta([], 0, "https://example.com/book")
        assert cta == "example.com/book"  # scheme stripped for clean display

    def test_none_cta_styles_falls_back_to_default_link(self):
        cta, _ = _pick_rotating_cta(None, 0, "http://example.com")
        assert cta == "example.com"

    def test_no_default_link_either_falls_back_to_link_in_bio(self):
        cta, nxt = _pick_rotating_cta([], 3, None)
        assert cta == "Link in bio"
        assert nxt == 3  # rotation index is untouched, not consumed

    def test_empty_string_default_link_also_falls_back_to_link_in_bio(self):
        cta, _ = _pick_rotating_cta([], 0, "")
        assert cta == "Link in bio"

    def test_fallback_rotation_index_is_unchanged_so_its_safe_to_always_persist(self):
        _, nxt = _pick_rotating_cta([], 5, "https://x.com")
        assert nxt == 5


class TestCallerSafety:
    def test_a_non_list_cta_styles_value_is_treated_as_empty(self):
        cta, _ = _pick_rotating_cta("not-a-list", 0, "https://x.com")
        assert cta == "x.com"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
