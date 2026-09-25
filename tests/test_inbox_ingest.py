"""Unified Inbox — webhook ingestion.

This endpoint has no JWT: Meta calls it directly, so the signature is the only thing
separating a real customer message from anything a stranger POSTs at the URL. These
cover that boundary, and the redelivery behaviour Meta's at-least-once guarantee makes
certain rather than hypothetical.
"""
import asyncio
import hashlib
import hmac
import json

import pytest

from app.agents.inbox.ingest import parse_meta_event, record_event, verify_signature

SECRET = "app-secret"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── The signature boundary ────────────────────────────────────────────────────

def test_a_correctly_signed_body_is_accepted():
    body = b'{"object":"page","entry":[]}'
    assert verify_signature(body, _sign(body), SECRET) is True


def test_a_tampered_body_is_rejected():
    """The signature covers the body. Changing one character must invalidate it —
    otherwise anyone could rewrite a real event in flight."""
    body = b'{"object":"page","entry":[]}'
    sig = _sign(body)
    assert verify_signature(body + b" ", sig, SECRET) is False


def test_the_wrong_secret_is_rejected():
    body = b'{"a":1}'
    assert verify_signature(body, _sign(body, "someone-elses-secret"), SECRET) is False


@pytest.mark.parametrize("header", ["", "abc", "sha1=deadbeef", "sha256=", "deadbeef"])
def test_missing_or_malformed_signatures_are_rejected(header):
    assert verify_signature(b"{}", header, SECRET) is False


def test_no_app_secret_configured_rejects_everything():
    """Failing OPEN here would mean an unconfigured deployment accepts forged events."""
    body = b"{}"
    assert verify_signature(body, _sign(body), "") is False


# ── Parsing what Meta actually sends ──────────────────────────────────────────

def test_a_direct_message_is_parsed():
    payload = {"entry": [{"id": "PAGE1", "messaging": [{
        "sender": {"id": "USER1"}, "timestamp": 1790000000000,
        "message": {"mid": "m_1", "text": "How much is the black bag?"}}]}]}
    ev = parse_meta_event(payload)[0]
    assert ev["type"] == "dm"
    assert ev["provider_message_id"] == "m_1"
    assert ev["text"] == "How much is the black bag?"
    assert ev["external_user_id"] == "USER1"


def test_our_own_sends_coming_back_are_ignored():
    """Meta echoes outbound messages to the same webhook. Storing them would show the
    agent their own reply as if the customer had sent it."""
    payload = {"entry": [{"id": "PAGE1", "messaging": [{
        "sender": {"id": "PAGE1"}, "message": {"mid": "m_2", "text": "hi", "is_echo": True}}]}]}
    assert parse_meta_event(payload) == []


def test_a_comment_is_parsed_with_its_post_as_the_thread():
    """A comment is not a DM: it belongs to an owned post, and every comment on that
    post belongs together."""
    payload = {"entry": [{"id": "PAGE1", "changes": [{"field": "comments", "value": {
        "comment_id": "c_1", "post_id": "p_9", "from": {"id": "U2", "name": "Ada"},
        "message": "my order hasn't arrived", "created_time": 1790000000}}]}]}
    ev = parse_meta_event(payload)[0]
    assert ev["type"] == "comment"
    assert ev["external_thread_id"] == "p_9"
    assert ev["display_name"] == "Ada"


def test_attribution_comes_only_from_the_provider():
    """"I saw your ad" is a sentence, not a link. A campaign id is attached when Meta
    supplies one and never inferred from wording (PRD §8.6)."""
    with_ad = parse_meta_event({"entry": [{"id": "P", "changes": [{"field": "comments",
        "value": {"comment_id": "c", "post_id": "p", "ad_id": "ad_7", "message": "x"}}]}]})[0]
    assert with_ad["source_ad_id"] == "ad_7"

    without = parse_meta_event({"entry": [{"id": "P", "changes": [{"field": "comments",
        "value": {"comment_id": "c2", "post_id": "p", "message": "I saw your ad"}}]}]})[0]
    assert without["source_ad_id"] == ""


def test_several_entries_in_one_post_all_come_through():
    """Meta batches. Handling only the first would silently drop customer messages."""
    payload = {"entry": [
        {"id": "P1", "messaging": [{"sender": {"id": "U1"}, "message": {"mid": "a", "text": "1"}}]},
        {"id": "P2", "messaging": [{"sender": {"id": "U2"}, "message": {"mid": "b", "text": "2"}}]},
    ]}
    assert len(parse_meta_event(payload)) == 2


def test_an_unrecognised_event_is_skipped_not_guessed_at():
    """A half-understood event in an agent's inbox is worse than a gap the health
    check reports."""
    assert parse_meta_event({"entry": [{"id": "P", "changes": [
        {"field": "mentions", "value": {"x": 1}}]}]}) == []


def test_milliseconds_and_seconds_timestamps_both_parse():
    ms = parse_meta_event({"entry": [{"id": "P", "messaging": [
        {"sender": {"id": "U"}, "timestamp": 1790000000000, "message": {"mid": "x", "text": "t"}}]}]})[0]
    sec = parse_meta_event({"entry": [{"id": "P", "changes": [{"field": "comments", "value": {
        "comment_id": "c", "post_id": "p", "message": "t", "created_time": 1790000000}}]}]})[0]
    assert ms["provider_timestamp"].year == sec["provider_timestamp"].year == 2026


# ── Stateful ingestion ────────────────────────────────────────────────────────
#
# A stand-in for Motor covering only the four operations ingest.py uses. It models
# the SEMANTICS that matter here — $setOnInsert applying once, return_document
# returning the pre-existing doc — so the assertions below are about ingest.py's
# logic, not Mongo's. The unique-index race it cannot model is covered by
# test_a_concurrent_duplicate_losing_the_index_race_is_swallowed.

import copy
from bson import ObjectId


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    @staticmethod
    def _matches(doc, q):
        return all(doc.get(k) == v for k, v in q.items())

    async def find_one(self, q, _projection=None):
        return next((copy.deepcopy(d) for d in self.docs if self._matches(d, q)), None)

    @staticmethod
    def _reject_empty_operators(update):
        """Mongo refuses `{"$set": {}}` with "'$set' is empty". The double must too,
        or it silently passes updates the real server would reject."""
        for op, body in update.items():
            if op.startswith("$") and not body:
                raise ValueError(f"'{op}' is empty. You must specify a field like so: "
                                 f"{{{op}: {{<field>: ...}}}}")

    async def find_one_and_update(self, q, update, upsert=False, return_document=False):
        self._reject_empty_operators(update)
        for d in self.docs:
            if self._matches(d, q):
                before = copy.deepcopy(d)
                d.update(update.get("$set", {}))
                return before if return_document else before
        if not upsert:
            return None
        doc = {"_id": ObjectId(), **update.get("$setOnInsert", {}), **update.get("$set", {})}
        self.docs.append(doc)
        return copy.deepcopy(doc)

    async def update_one(self, q, update):
        for d in self.docs:
            if self._matches(d, q):
                d.update(update.get("$set", {}))
                return

    async def insert_one(self, doc):
        doc = {"_id": ObjectId(), **doc}
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()


class FakeDB:
    def __init__(self):
        self.cols: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


from app.agents.inbox.entities import CONVERSATIONS, IDENTITIES, MESSAGES

WS = "ws_1"
ACCT = "acct_1"


def _dm(mid="m_1", text="hello", user="U1", thread="U1", ts=None):
    return {"type": "dm", "provider_message_id": mid, "text": text,
            "external_user_id": user, "external_thread_id": thread,
            "display_name": "Ada", "provider_timestamp": ts or now_dt()}


def now_dt():
    from datetime import datetime, timezone
    return datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def test_the_same_webhook_delivered_twice_stores_one_message():
    """Meta's delivery guarantee is at-least-once, so this is the normal case, not an
    edge case. A duplicate would show the agent the same customer message twice."""
    db = FakeDB()
    first = _run(record_event(db, WS, ACCT, "instagram", _dm()))
    second = _run(record_event(db, WS, ACCT, "instagram", _dm()))
    assert first is not None
    assert second is None
    assert len(db[MESSAGES].docs) == 1


def test_two_different_messages_both_store():
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_1")))
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_2", text="second")))
    assert len(db[MESSAGES].docs) == 2


def test_an_event_with_no_provider_id_is_dropped():
    """With nothing to deduplicate on, a retry would duplicate it forever."""
    db = FakeDB()
    ev = _dm(); ev["provider_message_id"] = ""
    assert _run(record_event(db, WS, ACCT, "instagram", ev)) is None
    assert db[MESSAGES].docs == []


def test_a_second_message_joins_the_same_conversation():
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_1")))
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_2", text="still there?")))
    assert len(db[CONVERSATIONS].docs) == 1
    assert len({m["conversation_id"] for m in db[MESSAGES].docs}) == 1


def test_a_customer_writing_again_reopens_a_resolved_thread():
    """PRD §4.2. An agent marked it done; the customer disagrees. Leaving it resolved
    hides real work."""
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_1")))
    db[CONVERSATIONS].docs[0]["status"] = "resolved"
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_2", text="hello?")))
    assert db[CONVERSATIONS].docs[0]["status"] == "open"


def test_an_open_thread_is_not_disturbed_by_new_activity():
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_1")))
    db[CONVERSATIONS].docs[0]["assignee_id"] = "agent_7"
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_2")))
    assert db[CONVERSATIONS].docs[0]["assignee_id"] == "agent_7"


def test_the_same_person_on_two_platforms_stays_two_identities():
    """Merging on a shared display name would show one customer another's thread."""
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "instagram", _dm(mid="m_1")))
    _run(record_event(db, WS, ACCT, "facebook", _dm(mid="m_2")))
    assert len(db[IDENTITIES].docs) == 2


def test_two_workspaces_do_not_share_a_conversation():
    """Same external thread id, different tenants. Isolation is per workspace."""
    db = FakeDB()
    _run(record_event(db, "ws_a", ACCT, "instagram", _dm(mid="m_1")))
    _run(record_event(db, "ws_b", ACCT, "instagram", _dm(mid="m_2")))
    assert len(db[CONVERSATIONS].docs) == 2


def test_the_same_provider_id_in_another_workspace_is_not_a_duplicate():
    db = FakeDB()
    assert _run(record_event(db, "ws_a", ACCT, "instagram", _dm(mid="m_1"))) is not None
    assert _run(record_event(db, "ws_b", ACCT, "instagram", _dm(mid="m_1"))) is not None


def test_a_stored_message_is_inbound_and_carries_its_text():
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "instagram", _dm(text="do you deliver to Yaba?")))
    m = db[MESSAGES].docs[0]
    assert m["direction"] == "inbound"
    assert m["text"] == "do you deliver to Yaba?"
    assert m["provider_timestamp"] == now_dt()


def test_a_concurrent_duplicate_losing_the_index_race_is_swallowed():
    """Two workers, same retry: the dedup read passes for both, then the unique index
    rejects the loser. That rejection IS the dedup working, not an error to raise."""
    db = FakeDB()

    async def boom(_doc):
        raise Exception("E11000 duplicate key error collection: inbox_messages")
    db[MESSAGES].insert_one = boom

    assert _run(record_event(db, WS, ACCT, "instagram", _dm())) is None


def test_an_unrelated_write_failure_is_not_swallowed():
    """Only the duplicate-key case is benign. Silently dropping a customer message on
    a real outage would be invisible."""
    db = FakeDB()

    async def boom(_doc):
        raise Exception("connection refused")
    db[MESSAGES].insert_one = boom

    with pytest.raises(Exception, match="connection refused"):
        _run(record_event(db, WS, ACCT, "instagram", _dm()))


def test_a_dm_without_a_display_name_still_stores():
    """Meta's messaging webhooks carry no name — only an id. Sending Mongo an empty
    $set would make that, the commonest event of all, fail outright."""
    db = FakeDB()
    ev = _dm()
    ev.pop("display_name")
    assert _run(record_event(db, WS, ACCT, "instagram", ev)) is not None
    assert db[IDENTITIES].docs[0]["external_user_id"] == "U1"


def test_a_name_is_recorded_when_the_provider_sends_one():
    db = FakeDB()
    _run(record_event(db, WS, ACCT, "facebook", _dm()))
    assert db[IDENTITIES].docs[0]["display_name"] == "Ada"
