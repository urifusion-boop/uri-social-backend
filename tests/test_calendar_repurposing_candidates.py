"""
_repurposing_candidate_concepts (content_calendar_v2_service.py) — PRD §23
"Content Repurposing Engine": recognize when one source can generate
multiple pieces. No raw asset-library/upload infrastructure exists anywhere
in this codebase (confirmed — see the function's own docstring), so this is
grounded in what's real and already queryable instead: a brand's own past
APPROVED content_drafts. Same zero-fabrication shape as
_listening_candidate_concepts (real text in, nothing invented) — mirrors
that file's test structure exactly. Uses an in-memory MongoDB; no network,
no AI — this function calls no model.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.content_calendar_v2.services.content_calendar_v2_service import _repurposing_candidate_concepts

USER_ID = "user-1"
BRAND_ID = "brand-1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["calendar_repurposing_test"]


async def _draft(db, draft_id, brand_id=BRAND_ID, approval_status="approved",
                  post_type="image", has_image=True, video_url=None,
                  content="Our customers told us switching to solar cut their monthly bills in half within weeks.",
                  created_at=None, **overrides):
    doc = {
        "id": draft_id, "brand_id": brand_id, "user_id": USER_ID,
        "approval_status": approval_status, "post_type": post_type,
        "has_image": has_image, "content": content,
        "created_at": created_at or datetime.utcnow(),
    }
    if video_url:
        doc["video_url"] = video_url
    doc.update(overrides)
    await db["content_drafts"].insert_one(dict(doc))


class TestRepurposingCandidateConcepts:
    def test_maps_a_real_approved_draft_into_the_candidate_shape(self):
        db = fresh_db()
        run(_draft(db, "d1"))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))

        assert len(concepts) == 1
        c = concepts[0]
        assert c["source"] == "repurposed"
        assert c["source_draft_id"] == "d1"
        assert c["territory"] == "E_PROOF"
        assert "cut their monthly bills in half" in c["listening_brief"]["observation"]
        # No invented fields — traces to the draft's own real content.
        assert set(c.keys()) >= {
            "territory", "subject", "angle", "creative_device", "format_hint",
            "objective", "audience_segment", "concept_name", "series_name",
        }

    def test_never_repeats_the_same_format_back(self):
        db = fresh_db()
        run(_draft(db, "d1", post_type="image"))
        run(_draft(db, "d2", post_type="carousel", content="A real carousel caption with plenty of real words in it."))
        run(_draft(db, "d3", post_type="video", video_url="https://x/v.mp4", has_image=False,
                    content="A real video caption with plenty of real words in it too."))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))
        by_id = {c["source_draft_id"]: c["format_hint"] for c in concepts}
        assert by_id["d1"] != "image"
        assert by_id["d2"] != "carousel"
        assert by_id["d3"] != "video"

    def test_unapproved_drafts_are_excluded(self):
        db = fresh_db()
        run(_draft(db, "d1", approval_status="pending"))
        run(_draft(db, "d2", approval_status="approved"))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))
        assert [c["source_draft_id"] for c in concepts] == ["d2"]

    def test_drafts_with_no_real_media_are_excluded(self):
        db = fresh_db()
        run(_draft(db, "d1", has_image=False, video_url=None))
        run(_draft(db, "d2", has_image=True))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))
        assert [c["source_draft_id"] for c in concepts] == ["d2"]

    def test_thin_content_is_excluded_as_not_real_source_material(self):
        db = fresh_db()
        run(_draft(db, "d1", content="Check it out!"))
        run(_draft(db, "d2", content="A genuinely substantial caption with enough real words to count."))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))
        assert [c["source_draft_id"] for c in concepts] == ["d2"]

    def test_limit_is_respected_and_most_recent_win(self):
        db = fresh_db()
        base = datetime(2026, 1, 1)
        for i in range(10):
            run(_draft(db, f"d{i}", created_at=base + timedelta(days=i)))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID, limit=3))
        assert len(concepts) == 3
        assert [c["source_draft_id"] for c in concepts] == ["d9", "d8", "d7"]

    def test_no_drafts_is_an_empty_list_not_an_error(self):
        db = fresh_db()
        assert run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID)) == []

    def test_only_this_brands_drafts_are_returned(self):
        db = fresh_db()
        run(_draft(db, "mine", brand_id=BRAND_ID))
        run(_draft(db, "someone_elses", brand_id="other-brand"))

        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))
        assert [c["source_draft_id"] for c in concepts] == ["mine"]

    def test_creative_device_never_forces_an_unconditional_video_override(self):
        # _assign_format treats category=="story" as an unconditional video
        # override regardless of format_hint — this would silently discard
        # the chosen new_format if ever set.
        db = fresh_db()
        run(_draft(db, "d1"))
        concepts = run(_repurposing_candidate_concepts(db, USER_ID, BRAND_ID))
        device = concepts[0]["creative_device"]
        assert device["category"] != "story"
        assert not (device["category"] == "conversational" and device["device"] == "talking_head")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
