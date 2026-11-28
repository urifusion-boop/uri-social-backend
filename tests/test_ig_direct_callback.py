"""
Instagram connect (Meta login) callback: Meta's access tokens must never reach the logs, and when
Meta shares Pages but no Instagram account the person is told what to do on Facebook's screen.
"""
import asyncio
import urllib.parse

import httpx

from app.agents.social_media_manager.routers import complete_social_manager as csm

USER_TOKEN = "EAAuserTOKENshouldNEVERbeLOGGED"
LONG_TOKEN = "EAAlongTOKENshouldNEVERbeLOGGED"
PAGE_TOKEN = "EAApageTOKENshouldNEVERbeLOGGED"


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def fake_client(page_fields: dict, declined=()):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **kw):
            return FakeResponse({"access_token": USER_TOKEN, "token_type": "bearer"})

        async def get(self, url, params=None, **kw):
            if url.endswith("/oauth/access_token"):
                return FakeResponse({"access_token": LONG_TOKEN, "token_type": "bearer"})
            if url.endswith("/me/accounts"):
                return FakeResponse({"data": [{"access_token": PAGE_TOKEN, "name": "We Love Ijekuje",
                                               "id": "107", "tasks": ["MANAGE"]}]})
            if url.endswith("/me/permissions"):
                return FakeResponse({"data": [
                    {"permission": "pages_show_list", "status": "granted"},
                    *({"permission": p, "status": "declined"} for p in declined),
                ]})
            if url.endswith("/107"):
                return FakeResponse({"id": "107", "name": "We Love Ijekuje", **page_fields})
            if url.endswith("/1784"):
                return FakeResponse({"id": "1784", "username": "welove"})
            raise AssertionError(f"unexpected call {url}")

    return FakeClient


class FakeDB(dict):
    def __getitem__(self, name):
        db = self

        class Coll:
            async def update_one(self, *a, **kw):
                db.setdefault("updated", []).append(a)

            async def insert_one(self, doc):
                db.setdefault("inserted", []).append(doc)

        return Coll()


def run_callback(monkeypatch, capsys, page_fields, declined=()):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: fake_client(page_fields, declined)())
    db = FakeDB()
    resp = asyncio.run(csm.instagram_direct_callback(code="abc", state="inbox", error=None,
                                                     error_reason=None, db=db))
    out = capsys.readouterr().out
    return urllib.parse.unquote(resp.headers["location"]), out, db


def test_no_token_is_logged(monkeypatch, capsys):
    location, out, _ = run_callback(monkeypatch, capsys, {"instagram_business_account": {"id": "1784"}})
    assert "connected=instagram_direct" in location
    for secret in (USER_TOKEN, LONG_TOKEN, PAGE_TOKEN):
        assert secret not in out
    assert "We Love Ijekuje" in out  # the diagnostics are still there


def test_page_without_instagram_explains_what_to_do(monkeypatch, capsys):
    location, out, _ = run_callback(monkeypatch, capsys, {})
    assert "connected=false" in location
    assert "We Love Ijekuje" in location and "tick your Instagram account" in location
    assert "instagram_business_account=None" in out
    assert PAGE_TOKEN not in out


def test_declined_instagram_permission_is_named(monkeypatch, capsys):
    location, out, _ = run_callback(monkeypatch, capsys, {}, declined=("instagram_basic",))
    assert "Instagram access wasn't allowed" in location
    assert "declined=['instagram_basic']" in out


def test_connected_instagram_account_is_used_when_business_link_is_empty(monkeypatch, capsys):
    location, _, db = run_callback(monkeypatch, capsys, {"connected_instagram_account": {"id": "1784"}})
    assert "connected=instagram_direct" in location and "ig_user_id=1784" in location
    assert db.get("updated")
