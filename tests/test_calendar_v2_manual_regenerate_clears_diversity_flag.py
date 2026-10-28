"""
Content Calendar V2 — manual single-item "Regenerate" must actually clear
(or correctly keep) the "similar" warning.

Confirmed live (user report + frontend read): the grid's warning triangle
is driven solely by item.diversity_check.passed === false
("Flagged as similar to another idea in this plan"). regenerate_item_v2
(the function behind the manual Regenerate button) never touched
diversity_check or creative_quality_review_note at all — so the warning
kept showing after a regenerate no matter what the new content said,
because nothing ever recomputed it. It also never passed avoid_context
into _generate_final_copy, the same blindness bug
test_calendar_v2_regeneration_avoids_rest_of_plan.py fixed for the
automatic regeneration pass.

Fix: regenerate_item_v2 now (1) passes the rest of the plan as
avoid_context, and (2) recomputes diversity_check +
creative_quality_review_note against the rest of the plan after
regenerating, using the same deterministic rule-based check the initial
plan generation already uses.
"""
import asyncio
from unittest.mock import AsyncMock, patch

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
    NotImplementedError) — real MongoDB does. Same minimal wrapper
    test_calendar_regenerate_scope.py already uses for regenerate_item_v2's
    exact update shape."""

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
        await self._real.replace_one(filter_, doc)


def fresh_db():
    client = mongomock_motor.AsyncMongoMockClient()["regen_diversity_test"]

    class _DB:
        def __getitem__(self, name):
            return _ArrayFilterCollection(client[name])
        def __getattr__(self, name):
            return getattr(client, name)
    return _DB()


def _item(day_index, title, **overrides):
    base = {
        "day_index": day_index, "date": f"2026-11-{day_index + 1:02d}",
        "title": title, "hook": f"Hook {day_index}", "cta": "Shop now",
        # Distinct per item by default so the adjacent territory+subject+
        # creative_angle rule doesn't spuriously fire and mask what each
        # test is actually isolating (title/visual-idea duplication).
        "territory": f"TERRITORY_{day_index}", "subject": f"subject {day_index}",
        "creative_angle": f"angle {day_index}",
        "creative_device": {"category": "story", "device": "day_in_the_life"},
        "format": "image", "primary_kpi": "engagement", "creative_concept_name": "Concept",
        "regenerated_count": 0, "reasoning": "x",
        "creative_direction": {"central_visual_idea": f"Visual idea {day_index}"},
        "diversity_check": {"passed": False, "similarity_score": 1.0, "flagged_against_item_id": None},
        "creative_quality_review_note": "stale note from initial generation",
    }
    base.update(overrides)
    return base


async def _seed_plan(db, items):
    await db[svc.COLLECTION].insert_one({
        "_id": PLAN_ID, "plan_id": PLAN_ID, "user_id": USER_ID, "brand_id": BRAND_ID,
        "status": "active", "platforms": ["instagram"], "items": items,
        "carousel_slots": [], "created_at": "2026-10-01T00:00:00", "updated_at": "2026-10-01T00:00:00",
    })


def _patch_common(regenerated_title, avoid_capture):
    async def fake_generate_final_copy(**kwargs):
        avoid_capture["avoid_context"] = kwargs.get("avoid_context")
        concept = kwargs["concepts_chunk"][0]
        return [{
            **concept, "title": regenerated_title, "hook": "New hook", "cta": "New CTA",
            "exact_copy": {"headline": regenerated_title, "caption": "x", "hashtags": []},
            "central_visual_idea": "A brand new visual idea", "reasoning": "x",
            "creative_concept_name": "Concept",
        }]

    return (
        patch.object(svc, "_generate_final_copy", side_effect=fake_generate_final_copy),
        patch(
            "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
            new=AsyncMock(return_value={"status": True, "responseData": {}}),
        ),
        patch(
            "app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.to_brand_context",
            side_effect=lambda raw: dict(raw) if raw else {},
        ),
        patch.object(svc, "_get_existing_assets_summary", new=AsyncMock(return_value="")),
    )


def test_regenerate_passes_the_rest_of_the_plan_as_avoid_context():
    db = fresh_db()
    items = [
        _item(0, "Title A"),
        _item(1, "Title B (flagged as similar)"),
        _item(2, "Title C"),
    ]
    run(_seed_plan(db, items))
    avoid_capture = {}
    p1, p2, p3, p4 = _patch_common("Genuinely New Title", avoid_capture)

    with p1, p2, p3, p4:
        run(svc.regenerate_item_v2(
            plan_id=PLAN_ID, item_index=1, user_id=USER_ID, db=db, brand_id=BRAND_ID,
        ))

    avoided_indices = {it["day_index"] for it in avoid_capture["avoid_context"]}
    assert avoided_indices == {0, 2}


def test_regenerate_clears_the_diversity_flag_when_new_title_no_longer_clashes():
    db = fresh_db()
    items = [
        _item(0, "Title A"),
        _item(1, "Title B (flagged as similar)"),  # starts flagged
        _item(2, "Title C"),
    ]
    run(_seed_plan(db, items))
    avoid_capture = {}
    p1, p2, p3, p4 = _patch_common("A Totally Different Headline", avoid_capture)

    with p1, p2, p3, p4:
        updated_plan = run(svc.regenerate_item_v2(
            plan_id=PLAN_ID, item_index=1, user_id=USER_ID, db=db, brand_id=BRAND_ID,
        ))

    item1 = next(it for it in updated_plan["items"] if it["day_index"] == 1)
    assert item1["title"] == "A Totally Different Headline"
    assert item1["diversity_check"]["passed"] is True   # no longer stuck on the stale flag
    assert item1["creative_quality_review_note"] is None


def test_regenerate_keeps_the_flag_when_new_title_still_clashes():
    db = fresh_db()
    items = [
        _item(0, "Why Your Shipping Costs Keep Rising"),
        _item(1, "Title B (flagged as similar)"),
        _item(2, "Title C"),
    ]
    run(_seed_plan(db, items))
    avoid_capture = {}
    # Regenerated title deliberately opens with the exact same first 5
    # words as item 0's title — the rule-based check must catch this and
    # keep the item flagged, rather than blindly clearing diversity_check
    # on every regenerate regardless of whether the new content still clashes.
    p1, p2, p3, p4 = _patch_common("Why Your Shipping Costs Keep Falling Instead", avoid_capture)

    with p1, p2, p3, p4:
        updated_plan = run(svc.regenerate_item_v2(
            plan_id=PLAN_ID, item_index=1, user_id=USER_ID, db=db, brand_id=BRAND_ID,
        ))

    item1 = next(it for it in updated_plan["items"] if it["day_index"] == 1)
    assert item1["diversity_check"]["passed"] is False
