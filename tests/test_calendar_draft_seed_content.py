"""
_build_draft_seed_content (content_calendar_v2_router.py) — the brief
handed from an approved calendar plan item to the real content generator
when the user clicks "Create Draft."

Confirmed missing (Oct 2026 brand-preference consistency audit): the
plan's own content_pillar/territory/audience_segment — the strategic
reasoning that got this specific idea selected in the first place — never
reached the real generator. It only ever saw the brand's whole content_
pillars list generically via brand_context, not which one THIS item was
actually planned under, so the downstream writer could plausibly land on
a different pillar/angle than the one the plan committed to.
"""
from app.agents.content_calendar_v2.routers.content_calendar_v2_router import _build_draft_seed_content


def _item(**overrides):
    base = {
        "title": "The upfront cost objection",
        "description": "Address the #1 reason SMEs hesitate on solar.",
        "content_pillar": "Affordability",
        "territory": "A_PROBLEM",
        "audience_segment": "Price-sensitive SME owners",
        "hook": "If your first thought is 'I can't afford that' — watch this.",
        "key_points": ["Financing exists", "Monthly cost beats a generator"],
        "caption_direction": "Lead with empathy, not a sales pitch.",
        "cta": "Send us a message",
        "exact_copy": {"caption": "Solar doesn't have to break the bank."},
        "creative_direction": {"central_visual_idea": "A split-screen: generator fumes vs. solar panels in sunlight."},
        "designer_execution_notes": "Keep the split-screen line dead-centre.",
    }
    base.update(overrides)
    return base


class TestStrategicFraming:
    def test_pillar_territory_and_audience_all_lead_the_seed(self):
        seed = _build_draft_seed_content(_item())
        assert seed.startswith("This post was planned as part of the calendar's")
        assert "content pillar: Affordability" in seed
        assert "creative territory: A_PROBLEM" in seed
        assert "audience segment: Price-sensitive SME owners" in seed

    def test_framing_line_comes_before_the_title(self):
        seed = _build_draft_seed_content(_item())
        framing_pos = seed.index("This post was planned")
        title_pos = seed.index("The upfront cost objection")
        assert framing_pos < title_pos

    def test_partial_framing_omits_the_missing_pieces(self):
        seed = _build_draft_seed_content(_item(territory=None, audience_segment=None))
        assert "content pillar: Affordability" in seed
        assert "creative territory" not in seed
        assert "audience segment" not in seed

    def test_no_framing_fields_at_all_skips_the_framing_line_entirely(self):
        seed = _build_draft_seed_content(_item(content_pillar=None, territory=None, audience_segment=None))
        assert "This post was planned" not in seed
        assert seed.startswith("The upfront cost objection")


class TestRestOfTheBrief:
    def test_every_other_field_still_carries_through(self):
        seed = _build_draft_seed_content(_item())
        assert "Opening hook to use: If your first thought" in seed
        assert "Key points to cover: Financing exists; Monthly cost beats a generator" in seed
        assert "Caption direction: Lead with empathy" in seed
        assert "Call to action: Send us a message" in seed
        assert "Publish-ready caption already written" in seed
        assert "Central visual idea: A split-screen" in seed
        assert "Production notes: Keep the split-screen line dead-centre." in seed

    def test_missing_optional_fields_are_simply_skipped(self):
        minimal = {"title": "T", "description": "D"}
        seed = _build_draft_seed_content(minimal)
        assert seed == "T. D"

    def test_empty_key_points_list_is_skipped_not_an_empty_line(self):
        seed = _build_draft_seed_content(_item(key_points=[]))
        assert "Key points to cover" not in seed


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
