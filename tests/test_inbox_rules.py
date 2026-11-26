"""Automation rules: what a comment or DM triggers, what is sent, and what must NOT fire."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.agents.inbox import rules
from app.agents.inbox.entities import CHANNEL_ACCOUNTS, CONVERSATIONS, IDENTITIES, MESSAGES, OUTBOX

WS = "ws"


def run(c):
    return asyncio.run(c)


class Transport:
    def __init__(self):
        self.sent = []

    async def __call__(self, conv, account, text):
        self.sent.append((conv.get("_reply_to"), text))
        return f"mid{len(self.sent)}"


class Private:
    def __init__(self):
        self.sent = []

    async def __call__(self, account, comment_id, text):
        self.sent.append((comment_id, text))
        return {"message_id": f"pm{len(self.sent)}", "recipient_id": "U9"}


async def no_llm(system, user):
    return {"id": None}


def raw(**over):
    base = {"kind": "comment", "match": {"keywords": ["price"]}, "public_reply": "Check your DM!"}
    base.update(over)
    return base


async def seed(db, kind="comment", user="U9", text="what is the price?", ad="", mid="c1"):
    await db[CHANNEL_ACCOUNTS].insert_one({"_id": ObjectId(), "external_account_id": "PAGE", "page_id": "PAGE"})
    acct = (await db[CHANNEL_ACCOUNTS].find_one({}))["_id"]
    ident = (await db[IDENTITIES].insert_one({"external_user_id": user, "display_name": "Zino Precious"})).inserted_id
    conv = (await db[CONVERSATIONS].insert_one({
        "workspace_id": WS, "platform": "instagram", "kind": kind, "channel_account_id": str(acct),
        "contact_identity_id": str(ident), "external_thread_id": "post1" if kind == "comment" else user,
        "source_ad_id": ad, "reply_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=20)})).inserted_id
    msg = (await db[MESSAGES].insert_one({
        "workspace_id": WS, "conversation_id": str(conv), "provider_message_id": mid, "direction": "inbound",
        "text": text, "received_at": datetime.now(timezone.utc)})).inserted_id
    return str(conv), str(msg)


def test_a_rule_needs_a_trigger_and_its_response():
    for bad in (raw(match={}), raw(public_reply=""), raw(kind="dm", dm=""), raw(kind="nope")):
        with pytest.raises(rules.RuleError):
            rules.normalize_rule(bad)
    assert rules.normalize_rule(raw(kind="comment_to_dm", public_reply="", dm="Here you go"))["public_reply"] == ""
    # "any message" is only a trigger when the rule is limited to an ad.
    with pytest.raises(rules.RuleError):
        rules.normalize_rule(raw(match={"any": True}))
    assert rules.normalize_rule(raw(match={"any": True}, ad_ids=["a1"]))["match"]["any"]


def test_keywords_match_whole_words_ignoring_case_and_punctuation():
    r = rules.normalize_rule(raw(match={"keywords": ["how much", "GUIDE"]}))
    assert rules.keyword_match(r, "Hi, HOW MUCH is it??")
    assert rules.keyword_match(r, "guide!")
    assert not rules.keyword_match(r, "guidebook")


def test_a_comment_rule_replies_publicly_to_that_comment():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(), "u")
        cid, mid = await seed(db)
        out = await rules.run_rules(db, WS, cid, mid, no_llm, t, Private())
        assert out["actions"] == ["public_reply"] and t.sent == [("c1", "Check your DM!")]
        assert (await db[rules.RULES].find_one({}))["fired_count"] == 1
    run(go())


def test_a_thread_from_before_a_reconnect_still_gets_rules():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(kind="dm", public_reply="", dm="Open 9 to 6", match={"keywords": ["hours"]}), "u")
        cid, mid = await seed(db, kind="dm", text="hours")
        fresh = (await db[CHANNEL_ACCOUNTS].insert_one(
            {"workspace_id": WS, "platform": "instagram", "external_account_id": "NEW", "page_id": "NEWP"})).inserted_id
        await db[CHANNEL_ACCOUNTS].delete_many({"external_account_id": "PAGE"})
        out = await rules.run_rules(db, WS, cid, mid, no_llm, t, Private())
        assert out["actions"] == ["dm"] and t.sent[0][1] == "Open 9 to 6"
        assert (await db[CONVERSATIONS].find_one({}))["channel_account_id"] == str(fresh)
    run(go())


def test_a_comment_to_dm_rule_beats_a_plain_comment_rule_on_the_same_word():
    async def go():
        db, t, pr = AsyncMongoMockClient()["t"], Transport(), Private()
        await rules.create_rule(db, WS, raw(match={"keywords": ["webinar"]}, public_reply="Check your DM"), "u")
        await rules.create_rule(db, WS, raw(kind="comment_to_dm", match={"keywords": ["webinar"]},
                                            public_reply="Check your DM", dm="Here is the link"), "u")
        cid, mid = await seed(db, text="Webinar")
        out = await rules.run_rules(db, WS, cid, mid, no_llm, t, pr)
        assert out["actions"] == ["public_reply", "dm"] and pr.sent == [("c1", "Here is the link")]
    run(go())


def test_a_second_commenter_on_the_same_post_is_answered_as_themselves():
    async def go():
        db, t, pr = AsyncMongoMockClient()["t"], Transport(), Private()
        await rules.create_rule(db, WS, raw(kind="comment_to_dm", match={"keywords": ["webinar"]}, dm="Hi {first_name}"), "u")
        cid, mid = await seed(db, text="Webinar", user="U1", mid="c1")
        assert (await rules.run_rules(db, WS, cid, mid, no_llm, t, pr))["actions"] == ["public_reply", "dm"]
        other = (await db[IDENTITIES].insert_one({"external_user_id": "U2", "display_name": "Ada Obi"})).inserted_id
        mid2 = (await db[MESSAGES].insert_one({
            "workspace_id": WS, "conversation_id": cid, "provider_message_id": "c2", "direction": "inbound",
            "text": "Webinar", "contact_identity_id": str(other), "received_at": datetime.now(timezone.utc)})).inserted_id
        out = await rules.run_rules(db, WS, cid, str(mid2), no_llm, t, pr)
        assert out and pr.sent[-1] == ("c2", "Hi Ada")
    run(go())


def test_comment_to_dm_sends_both_and_files_the_dm_thread():
    async def go():
        db, t, p = AsyncMongoMockClient()["t"], Transport(), Private()
        await rules.create_rule(db, WS, raw(kind="comment_to_dm", match={"keywords": ["guide"]},
                                            public_reply="Sent!", dm="Hi {first_name}, here it is: x.co/g"), "u")
        cid, mid = await seed(db, text="GUIDE")
        out = await rules.run_rules(db, WS, cid, mid, no_llm, t, p)
        assert out["actions"] == ["public_reply", "dm"]
        assert p.sent == [("c1", "Hi Zino, here it is: x.co/g")]
        dm = await db[CONVERSATIONS].find_one({"kind": "dm"})
        assert dm["external_thread_id"] == "U9" and dm["reply_window_expires_at"] is None
        assert (await db[MESSAGES].find_one({"conversation_id": str(dm["_id"])}))["direction"] == "outbound"
        # Meta allows one private reply per comment: running again sends nothing more.
        await rules.run_rules(db, WS, cid, mid, no_llm, t, p)
        assert len(p.sent) == 1
    run(go())


def test_an_off_rule_and_other_channels_do_not_fire():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(enabled=False), "u")
        await rules.create_rule(db, WS, raw(kind="dm", dm="Hello", match={"keywords": ["price"]}), "u")
        cid, mid = await seed(db)
        assert await rules.run_rules(db, WS, cid, mid, no_llm, t, Private()) is None
        assert t.sent == []
    run(go())


def test_our_own_reply_never_triggers_a_rule():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(match={"keywords": ["dm"]}), "u")
        cid, mid = await seed(db, user="PAGE", text="Check your DM!")
        assert await rules.run_rules(db, WS, cid, mid, no_llm, t, Private()) is None
    run(go())


def test_the_same_person_gets_a_comment_rule_once_per_post_per_day():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(), "u")
        cid, mid = await seed(db)
        assert await rules.run_rules(db, WS, cid, mid, no_llm, t, Private())
        again = (await db[MESSAGES].insert_one({
            "workspace_id": WS, "conversation_id": cid, "provider_message_id": "c2", "direction": "inbound",
            "text": "price??", "received_at": datetime.now(timezone.utc)})).inserted_id
        assert await rules.run_rules(db, WS, cid, str(again), no_llm, t, Private()) is None
    run(go())


def test_a_team_member_handling_the_conversation_keeps_rules_out():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(kind="dm", dm="Hello", match={"keywords": ["price"]}), "u")
        cid, mid = await seed(db, kind="dm")
        await db[CONVERSATIONS].update_one({"_id": ObjectId(cid)}, {"$set": {"assignee_id": "someone"}})
        assert await rules.run_rules(db, WS, cid, mid, no_llm, t) is None
    run(go())


def test_ad_rules_only_fire_for_that_ad_and_example_questions_match_by_meaning():
    async def go():
        db, t = AsyncMongoMockClient()["t"], Transport()
        await rules.create_rule(db, WS, raw(kind="dm", dm="Ad price: N25,000", ad_ids=["ad1"],
                                            match={"examples": ["how much is it"]}), "u")
        rid = (await db[rules.RULES].find_one({}))["rule_id"]

        async def llm(system, user):
            return {"id": rid}
        c1, m1 = await seed(db, kind="dm", text="abeg wetin be the cost", ad="other")
        assert await rules.run_rules(db, WS, c1, m1, llm, t) is None
        c2, m2 = await seed(db, kind="dm", text="abeg wetin be the cost", ad="ad1", user="U2", mid="d2")
        assert (await rules.run_rules(db, WS, c2, m2, llm, t))["actions"] == ["dm"]
        assert t.sent == [(None, "Ad price: N25,000")]
    run(go())


def test_account_for_follows_a_reconnect_but_never_guesses_a_whatsapp_number():
    from app.agents.inbox.accounts import account_for

    async def go():
        db = AsyncMongoMockClient()["t"]
        new = (await db[CHANNEL_ACCOUNTS].insert_one(
            {"workspace_id": WS, "platform": "instagram", "connected_at": datetime.now(timezone.utc)})).inserted_id
        await db[CHANNEL_ACCOUNTS].insert_one({"workspace_id": WS, "platform": "whatsapp"})
        conv_id = (await db[CONVERSATIONS].insert_one(
            {"workspace_id": WS, "platform": "instagram", "channel_account_id": str(ObjectId())})).inserted_id
        old = {"_id": conv_id, "workspace_id": WS, "platform": "instagram", "channel_account_id": str(ObjectId())}
        assert (await account_for(db, old))["_id"] == new
        assert (await db[CONVERSATIONS].find_one({"_id": conv_id}))["channel_account_id"] == str(new)
        assert await account_for(db, {**old, "platform": "whatsapp"}) is None
    run(go())
