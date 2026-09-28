"""Unified Inbox — connecting accounts.

The id confusion these cover is the kind that loses real customer messages silently:
Meta routes Instagram webhooks by the Instagram account id but accepts replies only
through the Page.
"""
import copy

import pytest

from app.agents.inbox.channels import account_for_event, channel_rows, link_workspace_channels
from app.agents.inbox.entities import CHANNEL_ACCOUNTS
from tests.test_inbox_ingest import FakeDB, _run
from tests.test_inbox_send import Db

IG_CONN = {
    "user_id": "u1", "brand_id": "ws_1", "platform": "instagram",
    "connection_status": "active", "ig_user_id": "IG99", "page_id": "PAGE7",
    "page_access_token": "tok", "username": "urisocial",
}


def test_one_instagram_connection_registers_both_the_account_and_its_page():
    """Instagram DMs arrive under the Instagram id; Messenger and Page comments arrive
    under the Page's. Registering one loses half the inbox."""
    rows = channel_rows(IG_CONN, "ws_1")
    by_platform = {r["platform"]: r for r in rows}
    assert by_platform["instagram"]["external_account_id"] == "IG99"
    assert by_platform["facebook"]["external_account_id"] == "PAGE7"


def test_the_instagram_row_still_sends_through_the_page():
    """Posting a reply to the Instagram user id fails; it has to go via the Page."""
    ig = next(r for r in channel_rows(IG_CONN, "ws_1") if r["platform"] == "instagram")
    assert ig["external_account_id"] == "IG99"
    assert ig["page_id"] == "PAGE7"


def test_a_connection_with_no_token_registers_nothing():
    """A row without a token would match a webhook and then fail every reply."""
    assert channel_rows({**IG_CONN, "page_access_token": ""}, "ws_1") == []


def test_a_personal_connection_resolves_to_the_personal_brand_id():
    """Personal connections carry no brand_id, and the workspace is NOT the bare
    user id — it is brnd_personal_<user_id>. Comparing against the raw user id
    matches nothing, which is exactly how this shipped returning linked: 0."""
    rows = channel_rows({**IG_CONN, "brand_id": None})
    assert rows[0]["workspace_id"] == "brnd_personal_u1"


def test_a_personal_connection_links_for_its_personal_workspace():
    db = _seeded_db([{**IG_CONN, "brand_id": None}])
    _run(link_workspace_channels(db, "u1", "brnd_personal_u1"))
    assert len(db[CHANNEL_ACCOUNTS].docs) == 2


def test_the_bare_user_id_is_not_treated_as_a_workspace():
    db = _seeded_db([{**IG_CONN, "brand_id": None}])
    _run(link_workspace_channels(db, "u1", "u1"))
    assert db[CHANNEL_ACCOUNTS].docs == []


def _seeded_db(conns):
    db = Db()
    # Copies: a test that mutates a connection must not edit the shared fixture
    # and change what every later test sees.
    db["social_connections"].docs.extend(copy.deepcopy(c) for c in conns)
    db["social_connections"].find = lambda q, *a, **k: _All(db["social_connections"].docs)
    return db


class _All:
    def __init__(self, docs): self.docs = docs
    def sort(self, *a, **k): return self
    def limit(self, *a): return self
    async def to_list(self, *a): return self.docs


def test_linking_registers_the_workspaces_accounts():
    db = _seeded_db([IG_CONN])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert len(db[CHANNEL_ACCOUNTS].docs) == 2


def test_linking_twice_updates_rather_than_duplicating():
    """Tokens get refreshed and Pages get reconnected. A second row could match the
    webhook instead and serve a stale token."""
    db = _seeded_db([IG_CONN])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    db["social_connections"].docs[0]["page_access_token"] = "tok2"
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert len(db[CHANNEL_ACCOUNTS].docs) == 2
    assert all(d["access_token"] == "tok2" for d in db[CHANNEL_ACCOUNTS].docs)


def test_another_workspaces_connection_is_not_linked():
    db = _seeded_db([{**IG_CONN, "brand_id": "ws_other"}])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert db[CHANNEL_ACCOUNTS].docs == []


def test_an_event_for_an_unconnected_account_finds_nothing():
    db = Db()
    assert _run(account_for_event(db, "NOPE")) is None
    assert _run(account_for_event(db, "")) is None


def test_an_instagram_webhook_finds_the_account_by_its_instagram_id():
    db = _seeded_db([IG_CONN])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    found = _run(account_for_event(db, "IG99"))
    assert found["platform"] == "instagram"
    assert found["page_id"] == "PAGE7"


def test_linking_returns_its_results_rather_than_stashing_them():
    """A module-level global would be shared between requests and hand one
    workspace's page ids to the next caller."""
    db = _seeded_db([IG_CONN])
    out = _run(link_workspace_channels(db, "u1", "ws_1"))
    assert set(out) == {"linked", "subscriptions"}
    assert len(out["linked"]) == 2


def test_a_user_token_is_exchanged_for_the_pages_own_token():
    """Some connection paths store a USER token under page_access_token, and Meta
    answers "(#210) A page access token is required" to anything Page-scoped —
    subscribing AND replying. The stored token has to be the Page's own."""
    import app.agents.inbox.channels as ch

    db = _seeded_db([IG_CONN])
    calls = {}

    async def fake_resolve(page_id, token):
        calls["resolve"] = (page_id, token)
        return "PAGE_TOKEN"

    async def fake_subscribe(page_id, token):
        calls["subscribe"] = (page_id, token)
        return True, ""

    original = (ch.resolve_page_token, ch.subscribe_page_to_app)
    ch.resolve_page_token, ch.subscribe_page_to_app = fake_resolve, fake_subscribe
    try:
        out = _run(ch.link_workspace_channels(db, "u1", "ws_1"))
    finally:
        ch.resolve_page_token, ch.subscribe_page_to_app = original

    assert calls["resolve"] == ("PAGE7", "tok")
    # The Page's token, not the one we started with.
    assert calls["subscribe"] == ("PAGE7", "PAGE_TOKEN")
    assert out["subscriptions"][0]["subscribed"] is True
    assert all(d["access_token"] == "PAGE_TOKEN" for d in db[CHANNEL_ACCOUNTS].docs)
