"""Unified Inbox — connecting accounts.

The id confusion these cover is the kind that loses real customer messages silently:
Meta routes Instagram webhooks by the Instagram account id but accepts replies only
through the Page.
"""
import copy

import pytest

from app.agents.inbox.channels import (account_for_event, begin_replace, channel_rows,
                                       finish_replace, in_sync_with_connections, link_workspace_channels,
                                       prune_stale_claims, release_channels, visible_rows)
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
    assert set(out) == {"linked", "subscriptions", "removed"}
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


def test_a_missing_messaging_scope_still_subscribes_comments():
    """Meta rejects the whole call if ONE field needs a permission the token
    lacks, so a missing pages_messaging would also cost us comments. Half a
    working inbox beats none."""
    import httpx

    import app.agents.inbox.channels as ch

    attempts = []

    class FakeResponse:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            attempts.append(data["subscribed_fields"])
            if "messages" in data["subscribed_fields"]:
                return FakeResponse(400, {"error": {"message":
                    "(#200) To subscribe to the messages field, one of these "
                    "permissions is needed: pages_messaging."}})
            return FakeResponse(200, {"success": True})

    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: FakeClient()
    try:
        ok, note = _run(ch.subscribe_page_to_app("PAGE7", "tok"))
    finally:
        httpx.AsyncClient = original

    assert ok is True
    assert "pages_messaging" in note
    assert attempts == ["messages,messaging_postbacks,message_reactions,feed",
                        "messages,messaging_postbacks,message_reactions", "feed"]


def test_a_missing_manage_metadata_still_delivers_dms():
    """pages_manage_metadata only gates `feed` (comments). A token without it must not
    cost the inbox its DMs, which is what Meta's all-or-nothing call would otherwise do."""
    import httpx

    import app.agents.inbox.channels as ch

    attempts = []

    class FakeResponse:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            attempts.append(data["subscribed_fields"])
            if "feed" in data["subscribed_fields"]:
                return FakeResponse(400, {"error": {"message":
                    "(#200) To subscribe to the feed field, one of these "
                    "permissions is needed: pages_manage_metadata"}})
            return FakeResponse(200, {"success": True})

    original = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: FakeClient()
    try:
        ok, note = _run(ch.subscribe_page_to_app("PAGE7", "tok"))
    finally:
        httpx.AsyncClient = original

    assert ok is True
    assert note.startswith("DMs only") and "pages_manage_metadata" in note
    assert attempts == ["messages,messaging_postbacks,message_reactions,feed",
                        "messages,messaging_postbacks,message_reactions"]


def test_an_account_claimed_by_two_workspaces_is_not_routed():
    """Meta says which ACCOUNT an event is for, never which workspace. Picking
    either one puts a real customer's message in another tenant's inbox."""
    db = Db()
    for ws in ("ws_a", "ws_b"):
        db[CHANNEL_ACCOUNTS].docs.append(
            {"workspace_id": ws, "platform": "facebook", "external_account_id": "PAGE7"})
    assert _run(account_for_event(db, "PAGE7")) is None


def test_the_same_workspace_twice_still_routes():
    """Duplicate rows within ONE workspace are not ambiguous — same destination."""
    db = Db()
    for _ in range(2):
        db[CHANNEL_ACCOUNTS].docs.append(
            {"workspace_id": "ws_a", "platform": "facebook", "external_account_id": "PAGE7"})
    found = _run(account_for_event(db, "PAGE7"))
    assert found is not None and found["workspace_id"] == "ws_a"


# ── Releasing a claim ─────────────────────────────────────────────────────────
# A channel row is a CLAIM on a provider account, and account_for_event refuses
# an account two workspaces claim. So a row that outlives its connection does not
# merely go stale — it breaks the inbox for whoever connects that Page next.

def test_disconnecting_releases_this_workspaces_claim():
    db = Db()
    db[CHANNEL_ACCOUNTS].docs.extend([
        {"workspace_id": "ws_1", "platform": "facebook", "external_account_id": "PAGE7"},
        {"workspace_id": "ws_1", "platform": "instagram", "external_account_id": "IG99"},
    ])
    assert _run(release_channels(db, "ws_1", external_account_id="PAGE7")) == 1
    assert [r["external_account_id"] for r in db[CHANNEL_ACCOUNTS].docs] == ["IG99"]


def test_releasing_leaves_another_workspaces_rows_alone():
    db = Db()
    db[CHANNEL_ACCOUNTS].docs.extend([
        {"workspace_id": "ws_1", "platform": "facebook", "external_account_id": "PAGE7"},
        {"workspace_id": "ws_2", "platform": "facebook", "external_account_id": "PAGE7"},
    ])
    _run(release_channels(db, "ws_1"))
    assert [r["workspace_id"] for r in db[CHANNEL_ACCOUNTS].docs] == ["ws_2"]


def test_linking_prunes_a_claim_no_connection_backs():
    """The Page moved to another workspace. The old claim has no connection behind
    it, and leaving it there makes every webhook for that Page ambiguous."""
    db = _seeded_db([{**IG_CONN, "brand_id": "ws_2"}])
    db[CHANNEL_ACCOUNTS].docs.append(
        {"workspace_id": "ws_1", "platform": "facebook", "external_account_id": "PAGE7"})
    _run(link_workspace_channels(db, "u1", "ws_2"))
    assert {r["workspace_id"] for r in db[CHANNEL_ACCOUNTS].docs} == {"ws_2"}
    assert _run(account_for_event(db, "PAGE7"))["workspace_id"] == "ws_2"


def test_linking_keeps_a_claim_a_live_connection_still_backs():
    """Two workspaces genuinely connected to one Page is a real conflict, not stale
    data — this must not resolve it by deleting somebody's live connection."""
    db = _seeded_db([{**IG_CONN, "brand_id": "ws_1"}])
    db[CHANNEL_ACCOUNTS].docs.append(
        {"workspace_id": "ws_1", "platform": "facebook", "external_account_id": "PAGE7"})
    _run(prune_stale_claims(db, "ws_2", "PAGE7"))
    assert len(db[CHANNEL_ACCOUNTS].docs) == 1


def test_the_inbox_drops_an_account_connected_accounts_no_longer_has():
    """Disconnected in Connected Accounts, or replaced by another account: the old
    channel must not linger as "connected" with a dead token."""
    from app.agents.inbox.channels import in_sync_with_connections
    db = _seeded_db([IG_CONN])
    db[CHANNEL_ACCOUNTS].docs.append({"_id": "old", "workspace_id": "ws_1", "platform": "instagram",
                                      "external_account_id": "OLD_IG", "access_token": "dead"})
    assert not _run(in_sync_with_connections(db, "ws_1"))
    out = _run(link_workspace_channels(db, "u1", "ws_1"))
    assert out["removed"] == 1
    assert "OLD_IG" not in {d["external_account_id"] for d in db[CHANNEL_ACCOUNTS].docs}
    assert _run(in_sync_with_connections(db, "ws_1"))


def test_whatsapp_is_not_touched_by_the_sync():
    db = _seeded_db([IG_CONN])
    db[CHANNEL_ACCOUNTS].docs.append({"_id": "wa", "workspace_id": "ws_1", "platform": "whatsapp",
                                      "external_account_id": "PN1", "access_token": "t"})
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert any(d["platform"] == "whatsapp" for d in db[CHANNEL_ACCOUNTS].docs)


def test_a_failed_page_subscription_is_recorded_on_the_channel():
    import app.agents.inbox.channels as ch

    db = _seeded_db([IG_CONN])

    async def fake_resolve(page_id, token):
        return token

    async def fake_subscribe(page_id, token):
        return False, "(#200) pages_messaging needed"

    original = (ch.resolve_page_token, ch.subscribe_page_to_app)
    ch.resolve_page_token, ch.subscribe_page_to_app = fake_resolve, fake_subscribe
    try:
        _run(ch.link_workspace_channels(db, "u1", "ws_1"))
    finally:
        ch.resolve_page_token, ch.subscribe_page_to_app = original

    subs = [d.get("subscription") for d in db[CHANNEL_ACCOUNTS].docs]
    assert subs and all(x and x["ok"] is False and "pages_messaging" in x["error"] for x in subs)


def test_every_token_for_a_page_is_tried_until_meta_accepts_one():
    """Facebook can connect through a provider whose token lacks the messaging
    permissions while Instagram's direct connect has them — the first token alone
    must not decide whether the Page is subscribed."""
    import app.agents.inbox.channels as ch

    fb_conn = {**IG_CONN, "platform": "facebook", "page_access_token": "weak", "ig_user_id": ""}
    db = _seeded_db([fb_conn, IG_CONN])
    tried = []

    async def fake_resolve(page_id, token):
        return token

    async def fake_subscribe(page_id, token):
        tried.append(token)
        return (token == "tok"), ("" if token == "tok" else "(#200) pages_messaging")

    original = (ch.resolve_page_token, ch.subscribe_page_to_app)
    ch.resolve_page_token, ch.subscribe_page_to_app = fake_resolve, fake_subscribe
    try:
        out = _run(ch.link_workspace_channels(db, "u1", "ws_1"))
    finally:
        ch.resolve_page_token, ch.subscribe_page_to_app = original

    assert tried == ["weak", "tok"]
    assert out["subscriptions"][0]["subscribed"] is True
    assert all(d["access_token"] == "tok" for d in db[CHANNEL_ACCOUNTS].docs)


def test_a_refused_subscription_is_explained_by_its_cause():
    from app.agents.inbox.channels import subscription_issue

    admin = subscription_issue("(#200) User does not have sufficient administrative permission for this action on this page.", "uri.social")
    assert admin["code"] == "not_page_admin" and "Full control" in admin["message"] and "uri.social" in admin["message"]
    perm = subscription_issue("(#200) one of these permissions is needed: pages_messaging.")
    assert perm["code"] == "missing_permission" and "Click Reconnect here" in perm["message"]
    assert subscription_issue("(#210) A page access token is required to request this resource.")["code"] == "not_page_admin"
    assert subscription_issue("") == {}
    assert subscription_issue("boom")["code"] == "subscription_failed"


def test_missing_metadata_permission_says_uri_will_ask_again():
    from app.agents.inbox.channels import subscription_issue
    out = subscription_issue("(#200) To subscribe to the feed field, one of these permissions is needed: "
                             "pages_manage_metadata", "Nayo's creation")
    assert out["code"] == "missing_permission" and "ask Meta for it again" in out["message"]


# ── Replacing an inbox account ───────────────────────────────────────────────

def test_a_connection_the_inbox_swapped_out_provides_no_rows_for_that_platform():
    rows = visible_rows({**IG_CONN, "inbox_hidden": ["instagram"]}, "ws_1")
    assert [r["platform"] for r in rows] == ["facebook"]


def test_linking_skips_what_the_inbox_swapped_out():
    db = _seeded_db([{**IG_CONN, "inbox_hidden": ["instagram"]}])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert [d["platform"] for d in db[CHANNEL_ACCOUNTS].docs] == ["facebook"]


class _Coll:
    def __init__(self, docs=None): self.docs = docs or []

    def find(self, q, *a, **k):
        docs = [d for d in self.docs if all(d.get(x) == y for x, y in q.items()
                                            if not isinstance(y, dict))]
        return _All(docs)

    async def find_one_and_delete(self, q):
        for d in self.docs:
            if all(d.get(x) == y for x, y in q.items()):
                self.docs.remove(d)
                return d
        return None

    async def update_one(self, q, update, upsert=False):
        doc = next((d for d in self.docs if all(d.get(x) == y for x, y in q.items())), None)
        if doc is None:
            if not upsert:
                return
            doc = dict(q)
            self.docs.append(doc)
        doc.update(update.get("$set", {}))
        for k, v in update.get("$addToSet", {}).items():
            if v not in doc.setdefault(k, []):
                doc[k].append(v)
        for k, v in update.get("$pull", {}).items():
            doc[k] = [x for x in doc.get(k, []) if x != v]


class _ReplaceDb(dict):
    def __missing__(self, name):
        self[name] = _Coll()
        return self[name]


def _replace_db(conns, shown):
    db = _ReplaceDb()
    db["social_connections"].docs.extend(copy.deepcopy(c) for c in conns)
    db[CHANNEL_ACCOUNTS].docs.extend({"workspace_id": "ws_1", "platform": "instagram",
                                      "external_account_id": i, "name": n} for i, n in shown)
    return db


NEW_CONN = {**IG_CONN, "ig_user_id": "IG2", "page_id": "PAGE2", "username": "preciouszino_",
            "_id": "c2", "updated_at": "2999-01-01T00:00:00"}
OLD_CONN = {**IG_CONN, "_id": "c1", "updated_at": "2020-01-01T00:00:00"}


def _swap(monkeypatch, db, platform="instagram"):
    import app.agents.inbox.channels as ch

    async def no_link(*a, **k): return {}
    monkeypatch.setattr(ch, "link_workspace_channels", no_link)
    _run(begin_replace(db, "ws_1", platform))
    return _run(finish_replace(db, "u1", "ws_1", platform))


def test_connecting_another_account_swaps_out_the_one_the_inbox_showed(monkeypatch):
    db = _replace_db([OLD_CONN], [("IG99", "urisocial")])
    _run(begin_replace(db, "ws_1", "instagram"))
    db["social_connections"].docs.append(copy.deepcopy(NEW_CONN))
    import app.agents.inbox.channels as ch

    async def no_link(*a, **k): return {}
    monkeypatch.setattr(ch, "link_workspace_channels", no_link)
    out = _run(finish_replace(db, "u1", "ws_1", "instagram"))
    assert out["replaced"] == ["urisocial"]
    old = next(c for c in db["social_connections"].docs if c["_id"] == "c1")
    new = next(c for c in db["social_connections"].docs if c["_id"] == "c2")
    assert old["inbox_hidden"] == ["instagram"]
    assert "inbox_hidden" not in new
    assert old["connection_status"] == "active"       # publishing still has it


def test_reconnecting_the_same_account_replaces_nothing(monkeypatch):
    db = _replace_db([OLD_CONN], [("IG99", "urisocial")])
    assert _swap(monkeypatch, db) == {"replaced": []}
    assert "inbox_hidden" not in db["social_connections"].docs[0]


def test_a_finish_with_no_begin_replaces_nothing(monkeypatch):
    db = _replace_db([OLD_CONN, NEW_CONN], [("IG99", "urisocial")])
    import app.agents.inbox.channels as ch

    async def no_link(*a, **k): return {}
    monkeypatch.setattr(ch, "link_workspace_channels", no_link)
    assert _run(finish_replace(db, "u1", "ws_1", "instagram")) == {"replaced": []}


def test_a_stale_begin_is_ignored(monkeypatch):
    from datetime import timedelta
    db = _replace_db([OLD_CONN, NEW_CONN], [("IG99", "urisocial")])
    _run(begin_replace(db, "ws_1", "instagram"))
    db["inbox_replace_pending"].docs[0]["at"] -= timedelta(hours=2)
    import app.agents.inbox.channels as ch

    async def no_link(*a, **k): return {}
    monkeypatch.setattr(ch, "link_workspace_channels", no_link)
    assert _run(finish_replace(db, "u1", "ws_1", "instagram")) == {"replaced": []}
    assert "inbox_hidden" not in db["social_connections"].docs[0]


def test_a_begin_for_another_platform_does_not_swap_this_one(monkeypatch):
    db = _replace_db([OLD_CONN, NEW_CONN], [("IG99", "urisocial")])
    _run(begin_replace(db, "ws_1", "facebook"))
    import app.agents.inbox.channels as ch

    async def no_link(*a, **k): return {}
    monkeypatch.setattr(ch, "link_workspace_channels", no_link)
    assert _run(finish_replace(db, "u1", "ws_1", "instagram")) == {"replaced": []}


def test_reconnecting_a_swapped_out_account_shows_it_again(monkeypatch):
    hidden = {**OLD_CONN, "inbox_hidden": ["instagram"], "updated_at": "2999-01-01T00:00:00"}
    db = _replace_db([hidden, {**NEW_CONN, "updated_at": "2020-01-01T00:00:00"}],
                     [("IG2", "preciouszino_")])
    out = _swap(monkeypatch, db)
    assert out["replaced"] == ["preciouszino_"]
    old = next(c for c in db["social_connections"].docs if c["_id"] == "c1")
    assert "instagram" not in old["inbox_hidden"]


# ── One account per platform ─────────────────────────────────────────────────

OLDER = {**IG_CONN, "_id": "a", "connected_at": "2026-01-01T00:00:00",
         "ig_user_id": "IG_OLD", "page_id": "PAGE_OLD", "username": "old"}
NEWER = {**IG_CONN, "_id": "b", "connected_at": "2026-06-01T00:00:00",
         "ig_user_id": "IG_NEW", "page_id": "PAGE_NEW", "username": "new"}


def test_the_inbox_shows_only_the_newest_account_per_platform():
    db = _seeded_db([OLDER, NEWER])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    ids = {(d["platform"], d["external_account_id"]) for d in db[CHANNEL_ACCOUNTS].docs}
    assert ids == {("instagram", "IG_NEW"), ("facebook", "PAGE_NEW")}


def test_connection_order_in_the_list_does_not_decide_which_wins():
    db = _seeded_db([NEWER, OLDER])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert {d["external_account_id"] for d in db[CHANNEL_ACCOUNTS].docs} == {"IG_NEW", "PAGE_NEW"}


def test_an_older_account_already_in_the_inbox_is_removed_when_a_newer_one_exists():
    db = _seeded_db([OLDER])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    db["social_connections"].docs.append(copy.deepcopy(NEWER))
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert {d["external_account_id"] for d in db[CHANNEL_ACCOUNTS].docs} == {"IG_NEW", "PAGE_NEW"}


def test_the_older_connection_stays_active_for_everything_else():
    db = _seeded_db([OLDER, NEWER])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert all(c["connection_status"] == "active" for c in db["social_connections"].docs)


def test_the_inbox_counts_as_in_sync_when_it_shows_only_the_newest():
    db = _seeded_db([OLDER, NEWER])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    assert _run(in_sync_with_connections(db, "ws_1")) is True


def test_instagram_and_facebook_each_keep_their_own_newest():
    page_only = {"user_id": "u1", "brand_id": "ws_1", "platform": "facebook", "_id": "c",
                 "connection_status": "active", "page_id": "PAGE_FB", "page_access_token": "t",
                 "page_name": "A Page", "connected_at": "2026-09-01T00:00:00"}
    db = _seeded_db([NEWER, page_only])
    _run(link_workspace_channels(db, "u1", "ws_1"))
    ids = {(d["platform"], d["external_account_id"]) for d in db[CHANNEL_ACCOUNTS].docs}
    assert ids == {("instagram", "IG_NEW"), ("facebook", "PAGE_FB")}
