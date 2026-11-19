"""The owner's fixed messages: the ad lead welcome and the away message. Sent once, only
on DMs, only inside the reply window, never over a team member, never with AI."""
import asyncio
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.agents.inbox import agent
from app.agents.inbox.entities import CONVERSATIONS, IDENTITIES, MESSAGES

# A Wednesday, 22:00 in Lagos (21:00 UTC) — outside 09:00–18:00.
NIGHT = datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


class Transport:
    def __init__(self):
        self.sent = []

    async def __call__(self, conv, account, text):
        self.sent.append(text)
        return f"mid{len(self.sent)}"


def settings(**over):
    s = agent.normalize_settings({})
    s["away_message"] = {"enabled": True, "text": "Hi {first_name}, we're closed right now."}
    s["ad_lead_welcome"] = {"enabled": True, "text": "Hi {first_name}, thanks for messaging from our ad!"}
    s.update(over)
    return s


async def seed(db, at, **conv):
    ident = ObjectId()
    await db[IDENTITIES].insert_one({"_id": ident, "display_name": "Zino Precious"})
    cid = ObjectId()
    await db[CONVERSATIONS].insert_one({
        "_id": cid, "workspace_id": "ws", "platform": "instagram", "kind": "dm",
        "contact_identity_id": str(ident), "channel_account_id": "",
        # The 24-hour window is checked against the real clock when sending.
        "reply_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=20), **conv})
    return str(cid)


def test_ad_lead_gets_the_welcome_once_with_their_name():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        cid = await seed(db, DAY, source_ad_id="ad1")
        assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, DAY) == ["ad_lead_welcome"]
        assert t.sent == ["Hi Zino, thanks for messaging from our ad!"]
        # The second message in the same conversation gets nothing more.
        assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, DAY + timedelta(minutes=5)) == []
        assert len(t.sent) == 1
    run(go())


def test_away_message_goes_once_per_closed_stretch():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        cid = await seed(db, NIGHT)
        assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, NIGHT) == ["away_message"]
        assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, NIGHT + timedelta(hours=2)) == []
        assert t.sent == ["Hi Zino, we're closed right now."]
        # Inside business hours: nothing.
        cid2 = await seed(db, DAY)
        assert await agent.send_fixed_replies(db, "ws", cid2, settings(), t, DAY) == []
    run(go())


def test_ad_lead_at_night_gets_the_welcome_not_both():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        cid = await seed(db, NIGHT, source_ad_id="ad1")
        assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, NIGHT) == ["ad_lead_welcome"]
        assert len(t.sent) == 1
    run(go())


def test_never_on_comments_assigned_or_human_handled_or_when_off():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        comment = await seed(db, NIGHT, kind="comment")
        assigned = await seed(db, NIGHT, assignee_id="u2")
        human = await seed(db, NIGHT)
        await db[MESSAGES].insert_one({"conversation_id": human, "direction": "outbound", "actor_id": "u2",
                                       "received_at": NIGHT - timedelta(hours=1)})
        for cid in (comment, assigned, human):
            assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, NIGHT) == []
        off = agent.normalize_settings({})
        assert await agent.send_fixed_replies(db, "ws", await seed(db, NIGHT), off, t, NIGHT) == []
        assert t.sent == []
    run(go())


def test_outside_the_reply_window_nothing_is_sent():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        cid = await seed(db, NIGHT, reply_window_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        assert await agent.send_fixed_replies(db, "ws", cid, settings(), t, NIGHT) == []
        assert t.sent == []
    run(go())


def test_per_day_hours_and_cleaning():
    s = agent.normalize_settings({"business_hours": {"day_hours": {"Sat": {"open": "10:00", "close": "14:00"},
                                                                   "Funday": {"open": "1", "close": "2"}}},
                                  "away_message": {"enabled": True, "text": "x" * 900}})
    assert s["business_hours"]["day_hours"] == {"Sat": {"open": "10:00", "close": "14:00"}}
    assert len(s["away_message"]["text"]) == agent.MAX_FIXED_REPLY_LEN
    sat_noon = datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc)   # 12:00 Lagos
    sat_late = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)   # 16:00 Lagos
    assert agent.within_business_hours(s["business_hours"], sat_noon)
    assert not agent.within_business_hours(s["business_hours"], sat_late)
    # Defaults keep both automations off.
    d = agent.normalize_settings({})
    assert not d["away_message"]["enabled"] and not d["ad_lead_welcome"]["enabled"]
