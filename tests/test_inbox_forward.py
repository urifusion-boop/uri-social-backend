"""Staging passes Meta deliveries to production so one webhook address serves both."""
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.agents.inbox import forward, router as inbox_router
from app.dependencies import get_db_dependency

SECRET = "app-secret"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def hook(monkeypatch):
    monkeypatch.setattr(inbox_router.settings, "META_APP_SECRET", SECRET, raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("META_WEBHOOK_FORWARD_URL", raising=False)
    app = FastAPI()
    app.include_router(inbox_router.router)
    db = AsyncMongoMockClient()["t"]
    app.dependency_overrides[get_db_dependency] = lambda: db
    with TestClient(app) as c:
        yield c


BODY = json.dumps({"object": "instagram", "entry": [{"id": "1", "messaging": []}]}).encode()


def _post(hook, headers=None):
    return hook.post("/inbox/webhooks/meta", content=BODY,
                       headers={"x-hub-signature-256": _sign(BODY), **(headers or {})})


def test_each_environment_forwards_in_its_own_direction(monkeypatch):
    monkeypatch.delenv("META_WEBHOOK_FORWARD_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    assert forward.forward_url(claimed=False) == ""
    assert forward.forward_url(claimed=True) == forward.STAGING_WEBHOOK_URL
    monkeypatch.setenv("APP_ENV", "development")
    assert forward.forward_url(claimed=False) == forward.PROD_WEBHOOK_URL
    assert forward.forward_url(claimed=True) == ""
    monkeypatch.setenv("APP_ENV", "")
    assert forward.forward_url(claimed=False) == "" and forward.forward_url(claimed=True) == ""


def test_production_trusts_any_answer_from_staging_but_staging_needs_production_to_own_it(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert forward.handled_remotely({"status": "ok", "stored": 0, "matched": 0}) is True
    assert forward.handled_remotely(None) is False
    monkeypatch.setenv("APP_ENV", "development")
    assert forward.handled_remotely({"matched": 0, "stored": 0}) is False
    assert forward.handled_remotely({"matched": 1}) is True


def test_ownership_uses_matched_and_falls_back_to_stored():
    assert forward.owned_by_forward_target({"matched": 1, "stored": 0}) is True
    assert forward.owned_by_forward_target({"matched": 0, "stored": 5}) is False
    assert forward.owned_by_forward_target({"stored": 1}) is True
    assert forward.owned_by_forward_target({"stored": 0}) is False
    assert forward.owned_by_forward_target(None) is False


def test_a_delivery_production_owns_is_not_processed_here(hook, monkeypatch):
    async def fake(url, raw, sig):
        return {"status": "ok", "events": 1, "stored": 1, "matched": 1}
    monkeypatch.setattr(inbox_router, "forward_delivery", fake)
    res = _post(hook)
    assert res.status_code == 200 and res.json()["status"] == "forwarded"


@pytest.mark.parametrize("remote", [None, {"status": "ok", "events": 1, "stored": 0, "matched": 0}])
def test_a_delivery_production_does_not_own_is_handled_here(hook, monkeypatch, remote):
    async def fake(url, raw, sig):
        return remote
    monkeypatch.setattr(inbox_router, "forward_delivery", fake)
    res = _post(hook)
    assert res.status_code == 200 and res.json()["status"] == "ok"


def test_a_forwarded_delivery_is_never_forwarded_again(hook, monkeypatch):
    async def boom(url, raw, sig):
        raise AssertionError("must not forward a forwarded delivery")
    monkeypatch.setattr(inbox_router, "forward_delivery", boom)
    assert _post(hook, {forward.FORWARD_HEADER: "1"}).status_code == 200


def test_a_bad_signature_is_rejected_before_any_forwarding(hook, monkeypatch):
    async def boom(url, raw, sig):
        raise AssertionError("unsigned traffic must not be forwarded")
    monkeypatch.setattr(inbox_router, "forward_delivery", boom)
    res = hook.post("/inbox/webhooks/meta", content=BODY, headers={"x-hub-signature-256": "sha256=bad"})
    assert res.status_code == 401


def test_a_staging_owned_workspace_is_answered_here_not_forwarded(hook, monkeypatch):
    import asyncio
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("META_STAGING_WORKSPACES", "ws-staging")

    async def lookup(account_id):
        return {"workspace_id": "ws-staging"} if account_id == "17841" else {"workspace_id": "ws-other"}

    events = [{"external_account_id": "17841"}]
    assert asyncio.get_event_loop().run_until_complete(forward.claimed_by_staging(events, lookup)) is True
    other = [{"external_account_id": "999"}]
    assert asyncio.get_event_loop().run_until_complete(forward.claimed_by_staging(other, lookup)) is False


def test_production_hands_a_staging_owned_delivery_to_staging_and_keeps_the_rest(hook, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    sent = []

    async def fake(url, raw, sig):
        sent.append(url)
        return {"status": "ok", "events": 1, "stored": 1}

    async def claimed(db, raw):
        return claimed.value

    monkeypatch.setattr(inbox_router, "forward_delivery", fake)
    monkeypatch.setattr(inbox_router, "_claimed_here", claimed)
    claimed.value = True
    assert _post(hook).json()["status"] == "forwarded" and sent == [forward.STAGING_WEBHOOK_URL]
    claimed.value = False
    assert _post(hook).json()["status"] == "ok" and len(sent) == 1


def test_production_handles_the_delivery_itself_when_staging_is_down(hook, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    async def down(url, raw, sig):
        return None

    async def claimed(db, raw):
        return True

    monkeypatch.setattr(inbox_router, "forward_delivery", down)
    monkeypatch.setattr(inbox_router, "_claimed_here", claimed)
    assert _post(hook).json()["status"] == "ok"
