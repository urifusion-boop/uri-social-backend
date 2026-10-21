"""
Per-platform visual style override resolution — _resolve_platform_style_pool
(app/agents/social_media_manager/routers/complete_social_manager.py).

Pure function, no I/O — a direct unit test exercises the real resolution
logic itself, not a mock of it. This is the mechanism the two
_generate_image_bg branches (carousel and regular post) both delegate to
instead of duplicating the same resolution inline.
"""
from app.agents.social_media_manager.routers.complete_social_manager import (
    _resolve_platform_style_pool,
)


def _brand_default_only(**overrides) -> dict:
    """A brand profile doc shape with only the flat (brand-wide) fields
    set — no per-platform overrides at all. Matches every existing brand
    profile as of this feature shipping."""
    base = {
        "style_selections": ["lifestyle_natural"],
        "style_prompt_fragments": ["frag-a"],
        "style_rotation_index": 2,
        "selected_custom_guides": ["guide-v1-a"],
        "selected_custom_guides_v2": ["guide-v2-a"],
    }
    base.update(overrides)
    return base


class TestNoOverrideFallsBackToBrandDefault:
    """A platform with nothing in the *_by_platform maps must behave
    exactly as if the feature didn't exist — this is what makes the
    feature safe to ship without a data migration."""

    def test_no_by_platform_maps_at_all(self):
        bp = _brand_default_only()
        resolved, field_path = _resolve_platform_style_pool(bp, "instagram")
        assert resolved["style_selections"] == ["lifestyle_natural"]
        assert resolved["selected_custom_guides"] == ["guide-v1-a"]
        assert resolved["selected_custom_guides_v2"] == ["guide-v2-a"]
        assert resolved["style_rotation_index"] == 2
        assert field_path == "style_rotation_index"

    def test_by_platform_maps_present_but_empty_for_this_platform(self):
        bp = _brand_default_only(
            style_selections_by_platform={"linkedin": ["corporate_clean"]},
        )
        resolved, field_path = _resolve_platform_style_pool(bp, "instagram")
        # linkedin has an override, but we asked for instagram — instagram
        # must still fall back to the brand default, not see linkedin's pool
        # or an empty one.
        assert resolved["style_selections"] == ["lifestyle_natural"]
        assert field_path == "style_rotation_index"

    def test_empty_list_override_counts_as_no_override(self):
        bp = _brand_default_only(
            style_selections_by_platform={"instagram": []},
            selected_custom_guides_by_platform={"instagram": []},
            selected_custom_guides_v2_by_platform={"instagram": []},
        )
        resolved, field_path = _resolve_platform_style_pool(bp, "instagram")
        assert resolved["style_selections"] == ["lifestyle_natural"]
        assert field_path == "style_rotation_index"


class TestOverrideResolution:
    def test_library_style_override_used_instead_of_default(self):
        bp = _brand_default_only(
            style_selections_by_platform={"instagram": ["bold_modern"]},
            style_rotation_index_by_platform={"instagram": 1},
        )
        resolved, field_path = _resolve_platform_style_pool(bp, "instagram")
        assert resolved["style_selections"] == ["bold_modern"]
        assert resolved["style_rotation_index"] == 1
        # No stored fragments for an override yet — falls back to a live
        # lookup downstream, an already-supported path.
        assert resolved["style_prompt_fragments"] == []
        assert field_path == "style_rotation_index_by_platform.instagram"

    def test_custom_guide_override_used_instead_of_default(self):
        bp = _brand_default_only(
            selected_custom_guides_by_platform={"linkedin": ["guide-v1-linkedin"]},
        )
        resolved, field_path = _resolve_platform_style_pool(bp, "linkedin")
        assert resolved["selected_custom_guides"] == ["guide-v1-linkedin"]
        # Only V1 was overridden for linkedin — V2 must come back empty for
        # this platform, not silently inherit the brand default's V2 guides.
        assert resolved["selected_custom_guides_v2"] == []
        assert field_path == "style_rotation_index_by_platform.linkedin"

    def test_rotation_index_defaults_to_zero_when_unset_for_this_platform(self):
        bp = _brand_default_only(
            style_selections_by_platform={"twitter": ["bold_modern"]},
            # No style_rotation_index_by_platform at all — first-ever use.
        )
        resolved, field_path = _resolve_platform_style_pool(bp, "twitter")
        assert resolved["style_rotation_index"] == 0
        assert field_path == "style_rotation_index_by_platform.twitter"

    def test_two_platforms_have_fully_independent_pools_and_rotation(self):
        bp = _brand_default_only(
            style_selections_by_platform={
                "instagram": ["bold_modern"],
                "linkedin": ["corporate_clean"],
            },
            style_rotation_index_by_platform={"instagram": 3, "linkedin": 0},
        )
        ig_resolved, ig_path = _resolve_platform_style_pool(bp, "instagram")
        li_resolved, li_path = _resolve_platform_style_pool(bp, "linkedin")

        assert ig_resolved["style_selections"] == ["bold_modern"]
        assert ig_resolved["style_rotation_index"] == 3
        assert ig_path == "style_rotation_index_by_platform.instagram"

        assert li_resolved["style_selections"] == ["corporate_clean"]
        assert li_resolved["style_rotation_index"] == 0
        assert li_path == "style_rotation_index_by_platform.linkedin"

        # A third, un-configured platform must still see the untouched
        # brand default, unaffected by either override existing.
        fb_resolved, fb_path = _resolve_platform_style_pool(bp, "facebook")
        assert fb_resolved["style_selections"] == ["lifestyle_natural"]
        assert fb_path == "style_rotation_index"

    def test_original_bp_dict_is_not_mutated(self):
        """The resolved dict must be a new object — callers (both
        _generate_image_bg branches) reassign _bp to the return value, but
        nothing should silently corrupt the original fetched-from-Mongo
        dict for any other concurrent reader of the same object."""
        bp = _brand_default_only(
            style_selections_by_platform={"instagram": ["bold_modern"]},
        )
        original_style_selections = bp["style_selections"]
        _resolve_platform_style_pool(bp, "instagram")
        assert bp["style_selections"] is original_style_selections
        assert bp["style_selections"] == ["lifestyle_natural"]
