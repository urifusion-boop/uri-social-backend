"""Playbook health: score, suggestions and the "used in" map."""
from datetime import datetime, timedelta

import pytest

from app.agents.social_media_manager.services import playbook_health as ph

NOW = datetime(2026, 10, 14)


class TestScore:
    def test_empty_playbook(self):
        result = ph.assess({}, now=NOW)
        assert result["score"] == 0 and result["level"] == "Needs attention"
        assert result["filled"] == 0 and result["missing_count"] == result["total"]

    def test_score_rises_with_important_fields(self):
        low = ph.assess({"tagline": "Fresh food, fast."}, now=NOW)["score"]         # weight 1
        high = ph.assess({"product_description": "Home-style food"}, now=NOW)["score"]  # weight 3
        assert 0 < low < high

    def test_full_playbook_is_100(self):
        profile = {f["key"]: "x" for f in ph.FIELDS}
        profile.update({
            "derived_voice": "warm", "guardrails": {"avoid_topics": "politics"},
            "business_pulse": {"current_promotions": ["20% off"]}, "business_pulse_updated_at": NOW,
            "content_pillars": ["Tips"], "cta_styles": ["Order now"], "style_selections": ["bold"],
        })
        result = ph.assess(profile, now=NOW)
        assert result["score"] == 100 and result["level"] == "Excellent" and result["suggestions"] == []


class TestSuggestions:
    def test_most_valuable_first_and_capped_at_five(self):
        result = ph.assess({}, now=NOW)
        assert len(result["suggestions"]) == 5
        weights = {f["key"]: f["weight"] for f in ph.FIELDS}
        assert all(weights[s["key"]] == 3 for s in result["suggestions"])
        assert result["suggestions"][0]["improves"]

    def test_filled_field_is_not_suggested(self):
        result = ph.assess({"brand_name": "Bukka", "logo_url": "https://x/logo.png"}, now=NOW)
        assert not {"brand_name", "logo_url"} & {s["key"] for s in result["suggestions"]}

    def test_quiz_or_derived_voice_counts_as_brand_voice(self):
        for profile in ({"derived_voice": "warm"}, {"personality_quiz": {"formality": "casual"}}):
            assert "brand_voice" not in {s["key"] for s in ph.assess(profile, now=NOW)["suggestions"]}

    def test_empty_guardrails_object_is_missing(self):
        assert not ph._filled({"guardrails": {"emoji_usage": "some", "avoid_topics": ""}}, "guardrails", NOW)
        assert ph._filled({"guardrails": {"compliance_notes": "no health claims"}}, "guardrails", NOW)

    def test_expired_promotions_are_flagged_for_update(self):
        profile = {f["key"]: "x" for f in ph.FIELDS}
        profile.update({"derived_voice": "warm", "guardrails": {"avoid_topics": "x"},
                        "business_pulse": {"current_promotions": ["Old promo"]},
                        "business_pulse_updated_at": NOW - timedelta(days=90)})
        suggestions = ph.assess(profile, now=NOW)["suggestions"]
        assert [s["key"] for s in suggestions] == ["business_pulse"]
        assert "over 45 days old" in suggestions[0]["why"]


class TestUsedIn:
    def test_every_field_and_alias_has_a_used_in(self):
        used = ph.used_in_by_label()
        for f in ph.FIELDS:
            assert used[f["label"]], f["label"]
        assert used["Topics to avoid"] == used["Guardrails"]
        assert used["Max hashtags"] == ["Posts"]

    def test_labels_are_unique(self):
        labels = [f["label"] for f in ph.FIELDS]
        assert len(labels) == len(set(labels))

    def test_guardrails_are_used_everywhere_jane_writes(self):
        used = ph.used_in_by_label()["Guardrails"]
        for job in ("Posts", "Calendar", "Ads", "Inbox", "Video", "Blog"):
            assert job in used


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
