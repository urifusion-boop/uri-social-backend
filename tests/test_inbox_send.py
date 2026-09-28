"""Unified Inbox — replying (PRD §8.4).

Sending is the half that reaches a real person. These cover the cases where the
tempting behaviour messages a customer twice, or at a moment Meta forbids.
"""
import asyncio
from datetime import timedelta

import pytest
from bson import ObjectId

from app.agents.inbox.entities import (
    AUDIT, CONVERSATIONS, MESSAGES, OUTBOX, now,
)
from app.agents.inbox.send import (
    ProviderTimeout, SendRefused, check_eligibility, reply_window_open, send_reply,
)
from tests.test_inbox_ingest import FakeDB, _run

WS = "ws_1"


class Db(FakeDB):
    """FakeDB plus the find().sort().limit().to_list() chain send.py uses."""

    def __getitem__(self, name):
        col = super().__getitem__(name)
        if not hasattr(col, "find"):
            col.find = lambda q, *a, **k: _Cursor(
                [d for d in col.docs if all(d.get(x) == y for x, y in q.items())])
        return col


class _Cursor:
    def __init__(self, docs): self.docs = docs
    def sort(self, *a, **k): return self
    def limit(self, *a): return self
    async def to_list(self, *a): return self.docs


def _conv(db, kind="dm", window_hours=6, platform="instagram"):
    doc = {
        "_id": ObjectId(), "workspace_id": WS, "kind": kind, "platform": platform,
        "channel_account_id": str(ObjectId()), "external_thread_id": "U1",
        "status": "open",
        "reply_window_expires_at": now() + timedelta(hours=window_hours),
    }
    db[CONVERSATIONS].docs.append(doc)
    return str(doc["_id"])


def ok_transport(mid="mid_1"):
    async def _t(conv, account, text):
        return mid
    return _t


async def boom_transport(conv, account, text):
    raise Exception("(#10) Message not sent outside the 24 hour window")


async def timeout_transport(conv, account, text):
    raise ProviderTimeout("read timeout")


async def silent_transport(conv, account, text):
    return ""


def _send(db, conv_id, text="hello", key="k1", transport=None):
    return _run(send_reply(db, WS, conv_id, text, key, "agent_1",
                           transport or ok_transport(), account={"access_token": "t"}))


# ── The reply window ──────────────────────────────────────────────────────────

def test_an_open_window_allows_a_reply():
    assert reply_window_open({"reply_window_expires_at": now() + timedelta(hours=1)})


def test_an_expired_window_does_not():
    assert not reply_window_open({"reply_window_expires_at": now() - timedelta(minutes=1)})


def test_an_unknown_window_refuses_rather_than_assuming_open():
    """Not-known is not the same as open. Guessing sends into a closed window and the
    rejection happens in front of the customer."""
    assert not reply_window_open({})
    with pytest.raises(SendRefused) as e:
        check_eligibility({"kind": "dm"})
    assert e.value.reason == "window_closed"


def test_a_comment_thread_has_no_window():
    """Public comments are not bound by the 24-hour messaging rule."""
    check_eligibility({"kind": "comment"})


def test_sending_outside_the_window_is_refused_before_the_provider_is_called():
    db = Db()
    conv_id = _conv(db, window_hours=-1)
    called = []

    async def spy(*a):
        called.append(1)
        return "mid"

    with pytest.raises(SendRefused) as e:
        _send(db, conv_id, transport=spy)
    assert e.value.reason == "window_closed"
    assert called == []
    assert db[OUTBOX].docs == []


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_the_same_idempotency_key_sends_once():
    """A double-clicked send button and a retried request look identical from here."""
    db = Db()
    conv_id = _conv(db)
    sent = []

    async def counting(conv, account, text):
        sent.append(text)
        return f"mid_{len(sent)}"

    first = _send(db, conv_id, key="same", transport=counting)
    second = _send(db, conv_id, key="same", transport=counting)

    assert len(sent) == 1
    assert second["provider_message_id"] == first["provider_message_id"]
    assert len(db[OUTBOX].docs) == 1


def test_different_keys_send_separately():
    db = Db()
    conv_id = _conv(db)
    _send(db, conv_id, key="a")
    _send(db, conv_id, key="b")
    assert len(db[OUTBOX].docs) == 2


# ── Delivery states ───────────────────────────────────────────────────────────

def test_a_timeout_is_recorded_as_unknown_not_failed():
    """FAILED invites a retry, and a timed-out send may already have arrived. UNKNOWN
    says a human must look."""
    db = Db()
    rec = _send(db, _conv(db), transport=timeout_transport)
    assert rec["delivery"] == "unknown"
    assert db[MESSAGES].docs == []


def test_a_rejection_is_recorded_as_failed_with_the_reason():
    db = Db()
    rec = _send(db, _conv(db), transport=boom_transport)
    assert rec["delivery"] == "failed"
    assert "24 hour window" in rec["failure_reason"]
    assert db[MESSAGES].docs == []


def test_a_provider_answer_with_no_id_is_not_treated_as_success():
    """Without an id there is nothing to reconcile against later."""
    db = Db()
    rec = _send(db, _conv(db), transport=silent_transport)
    assert rec["delivery"] == "unknown"
    assert db[MESSAGES].docs == []


def test_a_successful_send_is_accepted_and_stored_as_outbound():
    db = Db()
    conv_id = _conv(db)
    rec = _send(db, conv_id, text="we deliver to Yaba")
    assert rec["delivery"] == "accepted"
    msg = db[MESSAGES].docs[0]
    assert msg["direction"] == "outbound"
    assert msg["text"] == "we deliver to Yaba"
    assert msg["provider_message_id"] == "mid_1"


def test_the_intent_is_recorded_even_when_the_provider_never_answers():
    """If the outbox row were written after the call, a timed-out send would be
    invisible and the natural fix would double-send."""
    db = Db()
    _send(db, _conv(db), transport=timeout_transport)
    assert len(db[OUTBOX].docs) == 1
    assert db[OUTBOX].docs[0]["text"] == "hello"


def test_every_send_is_audited():
    db = Db()
    _send(db, _conv(db))
    assert db[AUDIT].docs[0]["actor_id"] == "agent_1"


# ── Scoping and validation ────────────────────────────────────────────────────

def test_another_workspaces_conversation_is_not_found():
    """Indistinguishable from a wrong id, deliberately."""
    db = Db()
    conv_id = _conv(db)
    db[CONVERSATIONS].docs[0]["workspace_id"] = "ws_other"
    with pytest.raises(SendRefused) as e:
        _send(db, conv_id)
    assert e.value.reason == "not_found"


def test_an_unparseable_id_is_refused_not_crashed():
    db = Db()
    with pytest.raises(SendRefused) as e:
        _send(db, "not-an-objectid")
    assert e.value.reason == "not_found"


@pytest.mark.parametrize("text", ["", "   ", None])
def test_an_empty_reply_is_refused(text):
    db = Db()
    with pytest.raises(SendRefused) as e:
        _send(db, _conv(db), text=text)
    assert e.value.reason == "empty"


def test_a_comment_reply_targets_the_comment_not_the_post():
    db = Db()
    conv_id = _conv(db, kind="comment")
    db[MESSAGES].docs.append({
        "_id": ObjectId(), "workspace_id": WS, "conversation_id": conv_id,
        "direction": "inbound", "provider_message_id": "comment_42",
    })
    seen = {}

    async def capture(conv, account, text):
        seen.update(conv)
        return "reply_1"

    _send(db, conv_id, transport=capture)
    assert seen["_reply_to"] == "comment_42"


def test_a_comment_thread_with_nothing_to_reply_to_is_refused():
    db = Db()
    with pytest.raises(SendRefused) as e:
        _send(db, _conv(db, kind="comment"))
    assert e.value.reason == "no_target"
