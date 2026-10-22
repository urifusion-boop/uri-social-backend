"""
_listening_candidate_concepts (content_calendar_v2_service.py) — Listening
PRD §18: "Content calendar & generator: Insight-backed post suggestions;
brief passed directly into generation." Real, already-validated Listening
opportunity cards join the same candidate pool the autonomous 30-day
planner scores and selects from — not a brainstormed idea, a reshape of
already-real fields. See the function's own docstring for why this isn't
the TrendDataService/PerformanceAnalyticsService this file's module
docstring otherwise bans. Uses an in-memory MongoDB; no network, no AI —
this function calls no model.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.content_calendar_v2.services.content_calendar_v2_service import _listening_candidate_concepts

USER_ID = "user-1"
BRAND_ID = "brand-1"
NOW = datetime(2026, 10, 22, 12, 0)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["calendar_listening_test"]


async def _card(db, card_id, status="shown", action_type="post", brand_id=BRAND_ID,
                expires_at=None, score=70, **overrides):
    doc = {
        "id": card_id, "brand_id": brand_id, "market": "hair_wigs:NG", "topic": "latasha wig",
        "topic_name": "Latasha wig", "type": "recurring_need", "status": status,
        "expires_at": expires_at or (NOW + timedelta(days=5)),
        "observation": "Customers keep asking about the Latasha wig in your market.",
        "why": "You sell lace and frontal wigs; this matches your catalogue.",
        "scores": {"total": score},
        "action": {
            "type": action_type, "title": "Film 3 ways to style the Latasha wig",
            "platforms": ["instagram", "tiktok"], "format": "Reel 30s",
            "hook": "Everyone's asking about this wig.",
            "outline": ["Show it styled two ways", "Answer the top question"],
            "cta": "DM to order",
        },
    }
    doc.update(overrides)
    await db["mi_listen_cards"].insert_one(dict(doc))


class TestListeningCandidateConcepts:
    def test_maps_a_real_card_into_the_candidate_shape(self):
        db = fresh_db()
        run(_card(db, "c1"))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID))

        assert len(concepts) == 1
        c = concepts[0]
        assert c["source"] == "listening"
        assert c["source_card_id"] == "c1"
        assert c["subject"] == "Latasha wig"
        assert c["territory"] == "A_PROBLEM"  # recurring_need
        assert c["angle"] == "The Customer's Question"
        assert c["concept_name"] == "Film 3 ways to style the Latasha wig"
        assert c["listening_brief"]["hook"] == "Everyone's asking about this wig."
        assert c["listening_brief"]["cta"] == "DM to order"
        # No invented fields — every value traces to the card's own real data.
        assert set(c.keys()) >= {
            "territory", "subject", "angle", "creative_device", "format_hint",
            "objective", "audience_segment", "concept_name", "series_name",
        }

    def test_action_type_maps_to_the_right_format_hint(self):
        db = fresh_db()
        run(_card(db, "c1", action_type="video"))
        run(_card(db, "c2", action_type="carousel", topic="other"))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID))

        by_id = {c["source_card_id"]: c for c in concepts}
        assert by_id["c1"]["format_hint"] == "video"
        assert by_id["c2"]["format_hint"] == "carousel"

    def test_ad_test_cards_are_skipped(self):
        db = fresh_db()
        run(_card(db, "c1", action_type="ad_test"))
        run(_card(db, "c2", action_type="post"))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID))

        assert [c["source_card_id"] for c in concepts] == ["c2"]

    def test_expired_cards_are_excluded(self):
        # The function reads real utcnow() internally (no injectable "now"),
        # so expiry has to be relative to actual wall-clock time, not NOW.
        db = fresh_db()
        run(_card(db, "c1", expires_at=datetime.utcnow() - timedelta(days=1)))
        run(_card(db, "c2", topic="other", expires_at=datetime.utcnow() + timedelta(days=5)))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID))

        assert [c["source_card_id"] for c in concepts] == ["c2"]

    def test_dismissed_or_already_acted_cards_are_excluded(self):
        db = fresh_db()
        run(_card(db, "c1", status="dismissed"))
        run(_card(db, "c2", status="acted"))
        run(_card(db, "c3", status="shown"))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID))

        assert [c["source_card_id"] for c in concepts] == ["c3"]

    def test_limit_is_respected_and_highest_scored_win(self):
        db = fresh_db()
        for i in range(10):
            run(_card(db, f"c{i}", topic=f"topic-{i}", score=i))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID, limit=3))

        assert len(concepts) == 3
        # Highest scores.total first.
        assert [c["source_card_id"] for c in concepts] == ["c9", "c8", "c7"]

    def test_no_cards_is_an_empty_list_not_an_error(self):
        db = fresh_db()
        assert run(_listening_candidate_concepts(db, USER_ID, BRAND_ID)) == []

    def test_only_this_brands_cards_are_returned(self):
        db = fresh_db()
        run(_card(db, "mine", brand_id=BRAND_ID))
        run(_card(db, "someone_elses", brand_id="other-brand", topic="other"))

        concepts = run(_listening_candidate_concepts(db, USER_ID, BRAND_ID))

        assert [c["source_card_id"] for c in concepts] == ["mine"]

    def test_personal_brand_falls_back_to_the_resolved_personal_brand_id(self):
        from app.models.brand_account import BrandAccount
        personal_id = BrandAccount.personal_brand_id(USER_ID)
        db = fresh_db()
        run(_card(db, "c1", brand_id=personal_id))

        concepts = run(_listening_candidate_concepts(db, USER_ID, None))

        assert [c["source_card_id"] for c in concepts] == ["c1"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
