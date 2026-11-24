"""Security audit 2026-11-23, critical findings 1-3.

1. The subscription payment webhook trusted unsigned "success" calls.
2. Magic-link generation (a working login for any user_id) needed no authentication.
3. Cron / daily-push endpoints compared against a CRON_SECRET that was never loaded,
   so they were open to anyone (prod logs show a scanner triggering them in Nov 2026).
"""
import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import shared_secret
from app.core.config import settings


def run(coro):
    return asyncio.run(coro)


# ── shared secret (findings 2 and 3) ─────────────────────────────────────────

def test_an_unset_secret_rejects_everyone():
    assert shared_secret.secret_matches("anything", "") is False
    assert shared_secret.secret_matches("", "") is False


def test_a_matching_secret_passes_and_a_wrong_one_does_not():
    assert shared_secret.secret_matches("s3cret", "s3cret") is True
    assert shared_secret.secret_matches("s3cre", "s3cret") is False


class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_cron_endpoints_are_closed_when_no_secret_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "CRON_SECRET", "")
    with pytest.raises(HTTPException) as e:
        shared_secret.require_cron_secret(_Req({"X-Cron-Secret": ""}))
    assert e.value.status_code == 403


def test_cron_endpoints_accept_the_configured_secret(monkeypatch):
    monkeypatch.setattr(settings, "CRON_SECRET", "cron-abc")
    shared_secret.require_cron_secret(_Req({"X-Cron-Secret": "cron-abc"}))
    with pytest.raises(HTTPException):
        shared_secret.require_cron_secret(_Req({"X-Cron-Secret": "guess"}))


@pytest.mark.parametrize("module, path", [
    ("app.agents.social_media_manager.routers.linkedin_router", "/daily-push"),
    ("app.agents.social_media_manager.routers.x_router", "/daily-push"),
    ("app.agents.social_media_manager.routers.whatsapp_router", "/daily-push"),
])
def test_daily_push_routes_refuse_callers_without_the_secret(monkeypatch, module, path):
    import importlib

    from app.dependencies import get_db_dependency

    monkeypatch.setattr(settings, "CRON_SECRET", "")
    mod = importlib.import_module(module)
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[get_db_dependency] = lambda: None
    prefix = mod.router.prefix or ""
    r = TestClient(app).post(path if path.startswith(prefix) else path)
    assert r.status_code in (403, 404)
    if r.status_code == 404:  # mounted under the router's own prefix
        assert TestClient(app).post(prefix + path).status_code == 403


def test_magic_link_generation_needs_the_internal_service_secret(monkeypatch):
    from app.dependencies import get_db_dependency
    from app.routers.auth_router import router

    monkeypatch.setattr(settings, "INTERNAL_SERVICE_SECRET", "svc-123")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_dependency] = lambda: None
    client = TestClient(app)
    path = (router.prefix or "") + "/magic-link/generate"
    body = {"user_id": "someone-else"}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={"X-Internal-Secret": "wrong"}).status_code == 403


# ── payment webhook (finding 1) ──────────────────────────────────────────────

SECRET = "sk_test_merchant"


def _service(monkeypatch):
    from app.services.PaymentService import PaymentService

    svc = PaymentService.__new__(PaymentService)

    async def creds():
        return {"secret_key": SECRET, "api_url": "https://squad.invalid"}

    monkeypatch.setattr(svc, "_get_squad_credentials", creds, raising=False)
    return svc


def _signed(body: dict):
    raw = json.dumps(body).encode()
    return raw, hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest().upper()


SUCCESS = {"Event": "charge_successful", "TransactionRef": "URI_ref_1",
           "Body": {"transaction_ref": "URI_ref_1", "transaction_status": "success"}}


def test_a_webhook_without_a_signature_is_rejected(monkeypatch):
    from app.services.PaymentService import WebhookSignatureError

    svc = _service(monkeypatch)
    with pytest.raises(WebhookSignatureError):
        run(svc.handle_webhook(payload=SUCCESS, signature=None, raw_body=json.dumps(SUCCESS).encode()))


def test_a_webhook_with_a_forged_signature_is_rejected(monkeypatch):
    from app.services.PaymentService import WebhookSignatureError

    svc = _service(monkeypatch)
    raw, _sig = _signed(SUCCESS)
    with pytest.raises(WebhookSignatureError):
        run(svc.handle_webhook(payload=SUCCESS, signature="ABC123", raw_body=raw))


def test_the_signature_covers_the_raw_bytes_not_a_reserialised_dict(monkeypatch):
    svc = _service(monkeypatch)
    raw = b'{"TransactionRef":"URI_ref_1",  "Event":"charge_successful"}'
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest().upper()
    assert run(svc.webhook_signature_valid(raw, sig)) is True
    assert run(svc.webhook_signature_valid(json.dumps(json.loads(raw)).encode(), sig)) is False


def test_a_signed_success_is_credited_only_after_squad_confirms_it(monkeypatch):
    svc = _service(monkeypatch)
    raw, sig = _signed(SUCCESS)
    calls = {"verify": [], "complete": 0}

    class _Coll:
        async def find_one(self, q):
            return {"transaction_ref": "URI_ref_1", "user_id": "u1", "status": "pending",
                    "subscription_tier": "starter"}

    monkeypatch.setattr(type(svc), "payment_transactions_collection", property(lambda self: _Coll()))

    async def verify(ref):
        calls["verify"].append(ref)
        return False   # Squad says it is not (yet) a successful payment

    async def complete(**_):
        calls["complete"] += 1

    monkeypatch.setattr(svc, "verify_payment", verify, raising=False)
    monkeypatch.setattr(svc, "_complete_payment", complete, raising=False)

    assert run(svc.handle_webhook(payload=SUCCESS, signature=sig, raw_body=raw)) is False
    assert calls["verify"] == ["URI_ref_1"]
    assert calls["complete"] == 0
