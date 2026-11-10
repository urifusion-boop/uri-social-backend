"""Campaign Builder wizard draft — one opaque-blob slot per brand (save/fetch/clear),
the backend half of the frontend's localStorage-first autosave (wizardDraft.ts)."""
from app.agents.jane_ads import wizard_draft as wd

from tests.test_jane_ads_growth import mongomock_motor, run  # noqa: F401


def test_save_then_load_round_trips_the_payload_exactly():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    payload = {"objective": {"value": "awareness"}, "platform": {"mode": "single", "single": "tiktok"}}
    run(wd.save(db, "brand1", payload))
    doc = run(wd.load(db, "brand1"))
    assert doc["payload"] == payload
    assert doc["updated_at"] is not None


def test_a_second_save_overwrites_the_same_slot_rather_than_adding_a_new_one():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save(db, "brand1", {"objective": {"value": "sales"}}))
    run(wd.save(db, "brand1", {"objective": {"value": "leads"}}))
    doc = run(wd.load(db, "brand1"))
    assert doc["payload"] == {"objective": {"value": "leads"}}


def test_drafts_are_scoped_per_brand():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save(db, "brand1", {"objective": {"value": "sales"}}))
    assert run(wd.load(db, "brand2")) is None


def test_load_is_none_when_nothing_was_ever_saved():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    assert run(wd.load(db, "brand1")) is None


def test_clear_removes_the_slot():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save(db, "brand1", {"objective": {"value": "sales"}}))
    run(wd.clear(db, "brand1"))
    assert run(wd.load(db, "brand1")) is None


def test_clear_on_a_brand_with_no_draft_does_not_raise():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.clear(db, "brand-with-nothing-saved"))
