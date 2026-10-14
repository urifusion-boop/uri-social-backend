"""The app must tell "your free trial ended" from "your plan's credits ran out"."""
import asyncio

import pytest

pytest.importorskip("mongomock_motor")
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.services.CreditService import CreditService  # noqa: E402


def service():
    svc = CreditService()
    db = AsyncMongoMockClient()["t"]
    svc._db = db  # the injection point CreditService provides for tests
    return svc, db


def run(coro):
    return asyncio.run(coro)


def test_trial_only_has_not_had_a_plan():
    svc, db = service()
    run(db["credit_transactions"].insert_one({"user_id": "u", "type": "trial", "reason": "trial"}))
    run(db["credit_transactions"].insert_one({"user_id": "u", "type": "deduction", "reason": "campaign_generation"}))
    assert run(svc.has_had_plan("u")) is False


@pytest.mark.parametrize("doc", [{"type": "allocation", "reason": "subscription"},
                                 {"type": "bonus", "reason": "custom_credit_purchase"}])
def test_paid_or_bought_credits(doc):
    svc, db = service()
    run(db["credit_transactions"].insert_one({"user_id": "u", **doc}))
    assert run(svc.has_had_plan("u")) is True


def test_access_code_plan_counts_even_after_it_ended():
    svc, db = service()
    run(db["access_code_redemptions"].insert_one({"user_id": "u", "code": "X", "revoked_at": "2026-10-13"}))
    assert run(svc.has_had_plan("u")) is True
    assert run(svc.has_had_plan("someone_else")) is False
