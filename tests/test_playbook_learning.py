"""The Playbook learns from the user's own edits and results."""
from datetime import datetime, timedelta

import pytest

from app.agents.social_media_manager.services import playbook_learning as pl


def edit(old, new, old_tags=None, new_tags=None, draft="d"):
    return {"draft_id": draft, "old": old, "new": new, "old_tags": old_tags, "new_tags": new_tags}


def ids(suggestions):
    return [s["id"] for s in suggestions]


class TestCollectEdits:
    def test_flattens_human_edits(self):
        drafts = [{"id": "d1", "human_edits": {
            "1": {"changes": {"content": {"old": "a", "new": "b"}}},
            "2": {"changes": {"hashtags": {"old": ["x", "y"], "new": ["x"]}}},
            "3": {"changes": {}},
        }}, {"id": "d2"}]
        edits = pl.collect_edits(drafts)
        assert len(edits) == 2
        assert edits[0]["old"] == "a" and edits[1]["new_tags"] == ["x"]


class TestEmoji:
    def _edits(self, stripped, kept):
        out = [edit(f"Fresh jollof today 🍛🔥 number {i}", f"Fresh jollof today number {i}", draft=f"s{i}") for i in range(stripped)]
        out += [edit(f"Party trays 🎉 batch {i}", f"Party trays 🎉 batch {i} now", draft=f"k{i}") for i in range(kept)]
        return out

    def test_habit_of_removing_emojis(self):
        s = pl.detect_from_edits(self._edits(4, 1), {})
        assert "emoji_none" in ids(s)
        assert "4 of the last 5" in s[0]["evidence"]
        assert s[0]["apply"] == {"guardrails": {"emoji_usage": "no"}}

    def test_not_a_habit_if_mostly_kept(self):
        assert "emoji_none" not in ids(pl.detect_from_edits(self._edits(2, 4), {}))

    def test_already_set(self):
        assert "emoji_none" not in ids(pl.detect_from_edits(self._edits(5, 0), {"guardrails": {"emoji_usage": "no"}}))

    def test_too_few_edits_says_nothing(self):
        assert pl.detect_from_edits(self._edits(3, 0), {}) == []


class TestHashtags:
    def _edits(self, n=4, keep=3):
        return [edit("", "", [f"t{j}" for j in range(8)], [f"t{j}" for j in range(keep)], draft=f"h{i}") for i in range(n)]

    def test_suggests_nearest_limit(self):
        s = pl.detect_from_edits(self._edits(4, 3), {})
        assert ids(s) == ["max_hashtags_3"]
        assert s[0]["apply"] == {"guardrails": {"max_hashtags": "3"}}

    def test_rounds_up_to_an_offered_option(self):
        assert ids(pl.detect_from_edits(self._edits(4, 4), {})) == ["max_hashtags_5"]

    def test_already_at_or_below_limit(self):
        assert pl.detect_from_edits(self._edits(4, 3), {"guardrails": {"max_hashtags": "3"}}) == []


class TestCaptionLength:
    LONG = "word " * 60

    def _edits(self):
        return [edit(self.LONG + str(i), "word " * 20 + str(i), draft=f"c{i}") for i in range(4)]

    def test_suggested_when_set_to_longer(self):
        s = pl.detect_from_edits(self._edits(), {"voice_profile": {"caption_length": "medium"}})
        assert "caption_short" in ids(s)

    def test_not_suggested_when_already_short(self):
        assert "caption_short" not in ids(pl.detect_from_edits(self._edits(), {"voice_profile": {"caption_length": "short"}}))


class TestWordsDeleted:
    def _edits(self, n=3):
        return [edit(f"Unlock amazing flavour at Bukka batch{i}", f"Taste real flavour at Bukka batch{i}", draft=f"w{i}")
                for i in range(n)] + [edit("Hello there friends", "Hello dear friends", draft="x")]

    def test_repeatedly_deleted_words(self):
        s = pl.detect_from_edits(self._edits(3), {"brand_name": "Bukka"})
        avoid = next(x for x in s if x["id"].startswith("avoid_"))
        assert avoid["apply"]["words_to_avoid_add"] == ["amazing", "unlock"]

    def test_brand_terms_are_never_suggested(self):
        edits = [edit(f"Order jollof from Bukka today batch{i}", f"Order today batch{i}", draft=f"b{i}") for i in range(4)]
        s = pl.detect_from_edits(edits, {"brand_name": "Bukka", "key_products_services": ["jollof rice"]})
        assert not any(x["id"].startswith("avoid_") for x in s)

    def test_word_the_user_also_writes_is_not_suggested(self):
        edits = self._edits(3) + [edit("Plain start here", "An amazing start here unlock", draft="y")]
        assert not any(x["id"].startswith("avoid_") for x in pl.detect_from_edits(edits, {}))

    def test_already_avoided(self):
        s = pl.detect_from_edits(self._edits(3), {"words_to_avoid": ["Amazing"], "guardrails": {"banned_words": "unlock"}})
        assert not any(x["id"].startswith("avoid_") for x in s)


class TestPostingTime:
    def _posts(self, evening_rate=8.0, other_rate=2.0, evening=4, other=8):
        base = datetime(2026, 10, 1)
        # Lagos is UTC+1: 17:00 UTC = 18:00 local (Evening); 09:00 UTC = 10:00 local (Late Morning).
        posts = [{"_published": base.replace(hour=17) + timedelta(days=i), "engagement_rate": evening_rate, "impressions": 500}
                 for i in range(evening)]
        posts += [{"_published": base.replace(hour=9) + timedelta(days=i), "engagement_rate": other_rate, "impressions": 500}
                  for i in range(other)]
        return posts

    def test_clear_winner_is_suggested_in_local_time(self):
        s = pl.detect_from_performance(self._posts(), {"region": "Nigeria"})
        assert ids(s) == ["post_time_evening_4_8pm"]
        assert s[0]["apply"] == {"posting_time_mode": "general", "posting_time_general": "Evening (4-8pm)"}
        assert "4 posts" in s[0]["evidence"]

    def test_needs_enough_posts(self):
        assert pl.detect_from_performance(self._posts(evening=2, other=3), {"region": "Nigeria"}) == []

    def test_one_lucky_post_is_not_a_pattern(self):
        assert pl.detect_from_performance(self._posts(evening=1, other=12), {"region": "Nigeria"}) == []

    def test_small_difference_is_ignored(self):
        assert pl.detect_from_performance(self._posts(evening_rate=2.2, other_rate=2.0), {"region": "Nigeria"}) == []

    def test_already_the_chosen_slot(self):
        profile = {"region": "Nigeria", "posting_time_mode": "general", "posting_time_prefs": {"general": "Evening (4-8pm),"}}
        assert pl.detect_from_performance(self._posts(), profile) == []


class TestDismissAndApply:
    def test_dismissed_stays_away_then_returns(self):
        now = datetime(2026, 10, 14)
        s = [{"id": "emoji_none"}, {"id": "caption_short"}]
        recent = {"playbook_suggestions_dismissed": {"emoji_none": now - timedelta(days=5)}}
        old = {"playbook_suggestions_dismissed": {"emoji_none": now - timedelta(days=90)}}
        assert ids(pl.filter_dismissed(s, recent, now=now)) == ["caption_short"]
        assert ids(pl.filter_dismissed(s, old, now=now)) == ["emoji_none", "caption_short"]

    def test_apply_keeps_the_rest_of_guardrails(self):
        update = pl.build_update({"guardrails": {"emoji_usage": "no"}}, {"guardrails": {"avoid_topics": "politics"}})
        assert update == {"guardrails": {"avoid_topics": "politics", "emoji_usage": "no"}}

    def test_apply_on_profile_without_guardrails(self):
        assert pl.build_update({"guardrails": {"max_hashtags": "5"}}, {"guardrails": None}) == {"guardrails": {"max_hashtags": "5"}}

    def test_apply_words_appends_without_duplicates(self):
        update = pl.build_update({"words_to_avoid_add": ["amazing", "unlock"]}, {"words_to_avoid": ["Amazing"]})
        assert update == {"words_to_avoid": ["Amazing", "unlock"]}

    def test_apply_posting_time_keeps_per_platform_slots(self):
        update = pl.build_update({"posting_time_mode": "general", "posting_time_general": "Evening (4-8pm)"},
                                 {"posting_time_prefs": {"linkedin": "Early Morning (6-9am),"}})
        assert update["posting_time_mode"] == "general"
        assert update["posting_time_prefs"] == {"linkedin": "Early Morning (6-9am),", "general": "Evening (4-8pm),"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
