"""
regenerate_item_v2 — PRD §46 "Step 13: User Editing": regenerate at
multiple levels, not just the whole item. Before this, only full-item
regeneration existed at all; "hook only", "copy only", "creative only" and
"format only" were all entirely missing. Uses an in-memory MongoDB; the one
real LLM call (_generate_final_copy) and the brand-profile lookup are
mocked — this tests the field-merging behavior per scope, not AI output
quality.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc

USER_ID = "user-1"
BRAND_ID = "brand-1"
PLAN_ID = "plan-1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _ArrayFilterCollection:
    """mongomock doesn't implement array_filters (confirmed: raises
    NotImplementedError) — real MongoDB does, and that's what production
    runs against. This thin wrapper simulates just enough of
    update_one(..., array_filters=[...]) / $push / $addToSet / $pull for
    regenerate_item_v2's exact update shape, delegating everything else
    (find_one, insert_one) straight to the real mongomock collection."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def update_one(self, filter_, update, array_filters=None, **kw):
        doc = await self._real.find_one(filter_)
        if not doc:
            return
        day_index = array_filters[0]["it.day_index"] if array_filters else None

        def _matches(item):
            return day_index is None or item.get("day_index") == day_index

        for key, value in (update.get("$set") or {}).items():
            if key.startswith("items.$[it]."):
                field = key.split(".", 2)[2]
                for item in doc["items"]:
                    if _matches(item):
                        item[field] = value
            else:
                doc[key] = value
        for key, value in (update.get("$push") or {}).items():
            if key == "items.$[it].version_history":
                for item in doc["items"]:
                    if _matches(item):
                        item.setdefault("version_history", []).append(value)
        for key, value in (update.get("$addToSet") or {}).items():
            doc.setdefault(key, [])
            if value not in doc[key]:
                doc[key].append(value)
        for key, value in (update.get("$pull") or {}).items():
            if key in doc and value in (doc[key] or []):
                doc[key].remove(value)
        await self._real.replace_one(filter_, doc)


def fresh_db():
    client = mongomock_motor.AsyncMongoMockClient()["calendar_regen_scope_test"]

    class _DB:
        def __getitem__(self, name):
            return _ArrayFilterCollection(client[name])
        def __getattr__(self, name):
            return getattr(client, name)
    return _DB()


def _original_item(**overrides):
    item = {
        "day_index": 0, "date": "2026-11-01", "territory": "A_PROBLEM", "subject": "Original subject",
        "creative_angle": "The Mistake", "creative_device": {"category": "visual", "device": "Close-up"},
        "format": "image", "primary_kpi": "engagement", "creative_concept_name": "Original Concept",
        "title": "Original Title", "description": "Original description.", "hook": "Original hook line here.",
        "key_points": ["Original point one here", "Original point two here"],
        "caption_direction": "Original caption direction text.", "keywords": ["orig"], "cta": "Original CTA.",
        "video_idea": {}, "exact_copy": {"headline": "Original headline", "caption": "Original caption.", "hashtags": ["orig"]},
        "carousel": None, "ai_image_prompt": "Original image prompt.", "design_style": "original_style",
        "layout_direction": "original layout", "visual_metaphor": "original metaphor",
        "required_assets": ["original asset"], "designer_execution_notes": "Original notes.",
        "reasoning": "Original reasoning.", "regenerated_count": 0,
        "creative_direction": {"visual_style": "original_style", "central_visual_idea": "original visual idea"},
    }
    item.update(overrides)
    return item


async def _plan(db, **item_overrides):
    doc = {
        "plan_id": PLAN_ID, "user_id": USER_ID, "brand_id": BRAND_ID, "status": "active",
        "platforms": ["instagram"], "carousel_slots": [],
        "items": [_original_item(**item_overrides)],
        "created_at": "2026-11-01T00:00:00", "updated_at": "2026-11-01T00:00:00",
    }
    await db[svc.COLLECTION].insert_one(dict(doc))


def _new_idea(**overrides):
    idea = {
        "title": "New Title", "description": "New description.", "hook": "Brand new hook line here.",
        "key_points": ["New point one here", "New point two here"],
        "caption_direction": "New caption direction text.", "keywords": ["new"], "cta": "New CTA.",
        "video_idea": {"format": "talking_head"},
        "exact_copy": {"headline": "New headline", "caption": "New caption.", "hashtags": ["new"]},
        "carousel": None, "ai_image_prompt": "New image prompt.", "design_style": "new_style",
        "layout_direction": "new layout", "visual_metaphor": "new metaphor",
        "required_assets": ["new asset"], "designer_execution_notes": "New notes.",
        "reasoning": "New reasoning.", "primary_kpi": "engagement", "creative_concept_name": "New Concept",
        "central_visual_idea": "new visual idea",
    }
    idea.update(overrides)
    return idea


def _mock_ai(idea_overrides=None):
    idea = _new_idea(**(idea_overrides or {}))

    async def fake(req):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps([idea])))])
    return fake


def _mock_brand_profile():
    return {"status": True, "responseData": {"brand_name": "Acme", "target_audience": "SMEs"}}


class TestRegenerateScope:
    def _run_regen(self, db, **kwargs):
        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=_mock_ai(kwargs.pop("idea_overrides", None))), \
             patch("app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
                   side_effect=lambda *a, **k: _mock_brand_profile()):
            return run(svc.regenerate_item_v2(PLAN_ID, 0, USER_ID, db, brand_id=BRAND_ID, **kwargs))

    def test_full_scope_replaces_everything(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(db, regen_scope="full")
        item = updated["items"][0]
        assert item["title"] == "New Title"
        assert item["hook"] == "Brand new hook line here."
        assert item["design_style"] == "new_style"
        assert item["ai_image_prompt"] == "New image prompt."

    def test_hook_scope_only_changes_hook_and_headline(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(db, regen_scope="hook")
        item = updated["items"][0]
        assert item["hook"] == "Brand new hook line here."
        assert item["exact_copy"]["headline"] == "New headline"
        # Everything else — including the rest of exact_copy — untouched.
        assert item["exact_copy"]["caption"] == "Original caption."
        assert item["exact_copy"]["hashtags"] == ["orig"]
        assert item["title"] == "Original Title"
        assert item["design_style"] == "original_style"
        assert item["ai_image_prompt"] == "Original image prompt."

    def test_copy_scope_keeps_creative_direction_untouched(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(db, regen_scope="copy")
        item = updated["items"][0]
        assert item["title"] == "New Title"
        assert item["hook"] == "Brand new hook line here."
        assert item["cta"] == "New CTA."
        # Creative direction fields must NOT move.
        assert item["design_style"] == "original_style"
        assert item["layout_direction"] == "original layout"
        assert item["visual_metaphor"] == "original metaphor"
        assert item["ai_image_prompt"] == "Original image prompt."
        assert item["required_assets"] == ["original asset"]
        assert item["designer_execution_notes"] == "Original notes."

    def test_creative_scope_keeps_copy_untouched(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(db, regen_scope="creative")
        item = updated["items"][0]
        assert item["design_style"] == "new_style"
        assert item["visual_metaphor"] == "new metaphor"
        assert item["ai_image_prompt"] == "New image prompt."
        # Copy must NOT move.
        assert item["title"] == "Original Title"
        assert item["hook"] == "Original hook line here."
        assert item["cta"] == "Original CTA."
        assert item["exact_copy"]["caption"] == "Original caption."

    def test_format_scope_converts_format_and_updates_carousel_slots(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(
            db, regen_scope="format", new_format="carousel",
            idea_overrides={"carousel": {"slides": [{"slide_index": 0, "headline": "S1", "body": "B1"},
                                                      {"slide_index": 1, "headline": "S2", "body": "B2"}]}},
        )
        item = updated["items"][0]
        assert item["format"] == "carousel"
        assert item["carousel"]["slides"]
        assert 0 in updated["carousel_slots"]

    def test_format_scope_requires_new_format(self):
        db = fresh_db()
        run(_plan(db))
        with pytest.raises(ValueError, match="new_format"):
            self._run_regen(db, regen_scope="format")

    def test_unknown_scope_is_rejected(self):
        db = fresh_db()
        run(_plan(db))
        with pytest.raises(ValueError, match="scope"):
            self._run_regen(db, regen_scope="not_a_real_scope")

    def test_version_history_records_the_scope_used(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(db, regen_scope="hook", reason="testing")
        version = updated["items"][0]["version_history"][-1]
        assert version["scope"] == "hook"
        assert version["reason"] == "testing"
        # Snapshot still captures the FULL pre-change state, so a revert
        # always restores everything, even though only the hook changed.
        assert version["snapshot"]["title"] == "Original Title"

    def test_regenerated_count_increments_regardless_of_scope(self):
        db = fresh_db()
        run(_plan(db))
        updated = self._run_regen(db, regen_scope="hook")
        assert updated["items"][0]["regenerated_count"] == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
