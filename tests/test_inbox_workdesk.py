"""The inbox desk: what the team does to a conversation is stored, scoped to the brand,
and validated — status, assignee, star, tags, lead, notes, quick replies, tag presets,
the audience list and the team list."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.agents.inbox import workdesk as wd
from app.agents.inbox.entities import CONVERSATIONS, IDENTITIES

ME = {"brand_id": "ws", "user_id": "u1"}


def run(coro):
    return asyncio.run(coro)


def fresh_db():
    return AsyncMongoMockClient()["t"]


async def seed(db, **extra):
    cid = ObjectId()
    await db[CONVERSATIONS].insert_one({"_id": cid, "workspace_id": "ws", "platform": "instagram",
                                        "kind": "dm", "status": "open", "tags": [], **extra})
    return str(cid)


def test_desk_changes_are_stored_and_read_back():
    async def go():
        db = fresh_db()
        await db["users"].insert_one({"userId": "u1", "fullName": "Ada Obi"})
        cid = await seed(db)
        out = await wd.update_desk(cid, {"status": "resolved", "starred": True, "tags": ["VIP", "vip", " Refund "],
                                         "lead_stage": "qualified", "lead_value": "39000", "assignee_id": "u1"},
                                   db, ME)
        assert out["status"] == "resolved" and out["starred"] and out["tags"] == ["VIP", "Refund"]
        assert out["lead_stage"] == "qualified" and out["lead_value"] == 39000.0 and out["assignee_id"] == "u1"
        doc = await db[CONVERSATIONS].find_one({"_id": ObjectId(cid)})
        assert doc["resolved_at"] and doc["starred"]
        audit = await db["inbox_audit_events"].find_one({"action": "inbox.desk"})
        assert audit and audit["actor_id"] == "u1"
    run(go())


@pytest.mark.parametrize("body", [{"status": "deleted"}, {"lead_stage": "maybe"}, {"lead_value": "lots"},
                                  {"lead_value": -5}, {"assignee_id": "stranger"}, {"tags": "VIP"}])
def test_bad_desk_values_are_refused(body):
    async def go():
        db = fresh_db()
        cid = await seed(db)
        with pytest.raises(HTTPException):
            await wd.update_desk(cid, body, db, ME)
    run(go())


def test_another_brand_cannot_touch_the_conversation():
    async def go():
        db = fresh_db()
        cid = await seed(db)
        with pytest.raises(HTTPException):
            await wd.update_desk(cid, {"starred": True}, db, {"brand_id": "other", "user_id": "u9"})
        with pytest.raises(HTTPException):
            await wd.add_note(cid, "hi", db, {"brand_id": "other", "user_id": "u9"})
    run(go())


def test_notes_are_added_with_the_author_and_can_be_deleted():
    async def go():
        db = fresh_db()
        await db["users"].insert_one({"userId": "u1", "fullName": "Ada Obi"})
        cid = await seed(db)
        note = await wd.add_note(cid, "  Called her, will pay Friday ", db, ME)
        assert note["text"] == "Called her, will pay Friday" and note["author_name"] == "Ada Obi"
        desk = await wd.get_desk(cid, db, ME)
        assert desk["notes_count"] == 1 and desk["notes"][0]["id"] == note["id"]
        assert (await wd.delete_note(cid, note["id"], db, ME))["deleted"]
        assert (await wd.get_desk(cid, db, ME))["notes"] == []
        with pytest.raises(HTTPException):
            await wd.add_note(cid, "   ", db, ME)
    run(go())


def test_quick_replies_and_tag_presets_persist_per_brand():
    async def go():
        db = fresh_db()
        first = await wd.get_prefs(db, ME)
        assert first["tag_presets"] == wd.DEFAULT_TAG_PRESETS and first["quick_replies"] == []
        saved = await wd.put_prefs({"quick_replies": [
            {"title": "Welcome", "text": "Hi {first_name}!", "shortcut": "hi", "category": "Greetings",
             "buttons": [{"label": "Create Ad Now", "kind": "create_ad"},
                         {"label": "Our site", "kind": "link", "url": "https://uri.ng"}]}],
            "tag_presets": ["VIP", "Wholesale"]}, db, ME)
        reply = saved["quick_replies"][0]
        assert reply["shortcut"] == "/hi" and reply["category"] == "Greetings" and len(reply["buttons"]) == 2
        await wd.quick_reply_used(reply["id"], db, ME)
        again = await wd.get_prefs(db, ME)
        assert again["quick_replies"][0]["uses"] == 1 and again["tag_presets"] == ["VIP", "Wholesale"]
        assert (await wd.get_prefs(db, {"brand_id": "other"}))["quick_replies"] == []
        # Removing a category keeps its replies, uncategorised.
        out = await wd.put_prefs({"categories": ["Pricing"]}, db, ME)
        assert out["quick_replies"][0]["category"] == ""
    run(go())


@pytest.mark.parametrize("reply", [
    {"title": "", "text": "x"},
    {"title": "A", "text": "x", "shortcut": "/has space"},
    {"title": "A", "text": "x", "buttons": [{"label": "Go", "kind": "link", "url": "not a url"}]},
])
def test_bad_quick_replies_are_refused(reply):
    async def go():
        with pytest.raises(HTTPException):
            await wd.put_prefs({"quick_replies": [reply]}, fresh_db(), ME)
    run(go())


def test_duplicate_shortcuts_are_refused():
    async def go():
        with pytest.raises(HTTPException):
            await wd.put_prefs({"quick_replies": [{"title": "A", "text": "x", "shortcut": "/a"},
                                                  {"title": "B", "text": "y", "shortcut": "/a"}]}, fresh_db(), ME)
    run(go())


def test_audience_groups_people_and_counts_segments():
    async def go():
        db = fresh_db()
        ada, tunde = ObjectId(), ObjectId()
        old = datetime.now(timezone.utc) - timedelta(days=30)
        await db[IDENTITIES].insert_many([
            {"_id": ada, "display_name": "Ada", "first_seen_at": datetime.now(timezone.utc)},
            {"_id": tunde, "display_name": "Tunde", "first_seen_at": old}])
        await seed(db, contact_identity_id=str(ada), lead_stage="qualified", source_ad_id="ad1",
                   tags=["VIP"], last_activity_at=datetime.now(timezone.utc))
        await seed(db, contact_identity_id=str(ada), tags=["Pricing"], last_activity_at=old)
        await seed(db, contact_identity_id=str(tunde), lead_stage="won", platform="whatsapp", last_activity_at=old,
                   needs_human=True)
        out = await wd.audience("all", "", "", 200, db, ME)
        assert out["counts"] == {"all": 2, "new": 1, "leads": 1, "customers": 1, "ads": 1, "follow_up": 1}
        ada_row = next(p for p in out["people"] if p["name"] == "Ada")
        assert ada_row["conversations"] == 2 and set(ada_row["tags"]) == {"VIP", "Pricing"} and ada_row["from_ad"]
        assert [p["name"] for p in (await wd.audience("customers", "", "", 200, db, ME))["people"]] == ["Tunde"]
        assert [p["name"] for p in (await wd.audience("all", "vip", "", 200, db, ME))["people"]] == ["Ada"]
        assert (await wd.audience("all", "", "", 200, db, {"brand_id": "other"}))["counts"]["all"] == 0
    run(go())


def test_team_lists_me_and_active_members_only():
    async def go():
        db = fresh_db()
        await db["users"].insert_many([{"userId": "u1", "fullName": "Ada Obi"}, {"userId": "u2", "email": "t@x.ng"}])
        await db["workspace_members"].insert_many([
            {"workspace_id": "ws", "user_id": "u2", "status": "active"},
            {"workspace_id": "ws", "user_id": "u3", "status": "suspended"}])
        members = (await wd.team(db, ME))["members"]
        assert [(m["user_id"], m["name"], m["is_me"]) for m in members] == [("u1", "Ada Obi", True), ("u2", "t@x.ng", False)]
    run(go())
