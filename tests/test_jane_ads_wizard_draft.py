"""Campaign Builder wizard drafts — one opaque-blob slot per (brand, draft_id)
pair, the backend half of the frontend's localStorage-first autosave
(wizardDraft.ts). Multiple drafts coexist per brand; each is addressed by its
own client-generated draft_id."""
from app.agents.jane_ads import wizard_draft as wd

from tests.test_jane_ads_growth import mongomock_motor, run  # noqa: F401


def test_save_then_list_round_trips_the_payload_exactly():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    payload = {"objective": {"value": "awareness"}, "platform": {"mode": "single", "single": "tiktok"}}
    run(wd.save_draft(db, "brand1", "draft-a", payload))
    docs = run(wd.list_drafts(db, "brand1"))
    assert len(docs) == 1
    assert docs[0]["payload"] == payload
    assert docs[0]["draft_id"] == "draft-a"
    assert docs[0]["updated_at"] is not None


def test_two_different_draft_ids_coexist_for_the_same_brand():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save_draft(db, "brand1", "draft-a", {"objective": {"value": "sales"}}))
    run(wd.save_draft(db, "brand1", "draft-b", {"objective": {"value": "leads"}}))
    docs = run(wd.list_drafts(db, "brand1"))
    assert {d["draft_id"] for d in docs} == {"draft-a", "draft-b"}


def test_a_second_save_of_the_same_draft_id_overwrites_not_duplicates():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save_draft(db, "brand1", "draft-a", {"objective": {"value": "sales"}}))
    run(wd.save_draft(db, "brand1", "draft-a", {"objective": {"value": "leads"}}))
    docs = run(wd.list_drafts(db, "brand1"))
    assert len(docs) == 1
    assert docs[0]["payload"] == {"objective": {"value": "leads"}}


def test_drafts_are_scoped_per_brand():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save_draft(db, "brand1", "draft-a", {"objective": {"value": "sales"}}))
    assert run(wd.list_drafts(db, "brand2")) == []


def test_list_is_empty_when_nothing_was_ever_saved():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    assert run(wd.list_drafts(db, "brand1")) == []


def test_delete_removes_only_that_one_draft():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.save_draft(db, "brand1", "draft-a", {"objective": {"value": "sales"}}))
    run(wd.save_draft(db, "brand1", "draft-b", {"objective": {"value": "leads"}}))
    run(wd.delete_draft(db, "brand1", "draft-a"))
    docs = run(wd.list_drafts(db, "brand1"))
    assert len(docs) == 1
    assert docs[0]["draft_id"] == "draft-b"


def test_delete_on_a_brand_with_no_matching_draft_does_not_raise():
    db = mongomock_motor.AsyncMongoMockClient()["wd"]
    run(wd.delete_draft(db, "brand1", "no-such-draft"))
