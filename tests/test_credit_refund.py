"""
Refunds must work on our DocumentDB clusters, which reject update PIPELINES (the list-style update that
refund_credit used to send). A refund that throws leaves a customer who paid for a video that never came.
The fake wallet collection below refuses a list-style update, exactly as those clusters do.
"""
import asyncio

import pytest

from app.services.CreditService import CreditService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeWallets:
    def __init__(self, doc=None, lose_first_race=False):
        self.doc = dict(doc) if doc else None
        self.lose_first_race = lose_first_race
        self.updates = 0

    async def find_one(self, flt, projection=None):
        return dict(self.doc) if self.doc and self.doc["user_id"] == flt["user_id"] else None

    @staticmethod
    def _matches(doc, flt):
        for key, want in flt.items():
            have = doc.get(key)
            if isinstance(want, dict) and "$in" in want:
                if have not in want["$in"]:
                    return False
            elif have != want:
                return False
        return True

    async def find_one_and_update(self, flt, update, return_document=None):
        if isinstance(update, list):
            raise RuntimeError("Failed to parse update: field must be of BSON type object")   # old DocumentDB
        self.updates += 1
        if self.lose_first_race and self.updates == 1:
            self.doc["bonus_credits"] += 1            # someone else spent or earned in between
            return None
        if not self.doc or not self._matches(self.doc, flt):
            return None
        self.doc.update(update["$set"])
        return dict(self.doc)


class FakeLog:
    def __init__(self):
        self.rows = []

    async def insert_one(self, row):
        self.rows.append(row)


def _service(doc=None, **kw):
    wallets, log = FakeWallets(doc, **kw), FakeLog()

    class Svc(CreditService):
        user_credits_collection = wallets
        credit_transactions_collection = log

    return Svc(), wallets, log


WALLET = {"user_id": "u1", "subscription_credits": 5, "bonus_credits": 2, "credits_used": 7,
          "total_credits": 7, "credits_remaining": 7}


def test_a_refund_adds_the_credits_back_and_logs_it():
    svc, wallets, log = _service(WALLET)
    assert _run(svc.refund_credit("u1", campaign_id="job1", reason="refund", amount=3)) is True
    assert wallets.doc["bonus_credits"] == 5 and wallets.doc["subscription_credits"] == 5
    assert wallets.doc["credits_remaining"] == 10 and wallets.doc["credits_used"] == 4
    assert log.rows[0]["type"] == "refund" and log.rows[0]["amount"] == 3
    assert (log.rows[0]["balance_before"], log.rows[0]["balance_after"]) == (7, 10)
    assert log.rows[0]["campaign_id"] == "job1"


def test_credits_used_never_goes_below_zero():
    svc, wallets, _ = _service({**WALLET, "credits_used": 1})
    _run(svc.refund_credit("u1", "job1", amount=3))
    assert wallets.doc["credits_used"] == 0


def test_a_user_with_no_wallet_gets_false_and_no_log():
    svc, _, log = _service(None)
    assert _run(svc.refund_credit("u1", "job1", amount=3)) is False
    assert log.rows == []


def test_a_balance_that_changed_underneath_is_retried_not_overwritten():
    svc, wallets, log = _service(WALLET, lose_first_race=True)
    assert _run(svc.refund_credit("u1", "job1", amount=3)) is True
    assert wallets.doc["bonus_credits"] == 6          # their +1 survived, our +3 landed on top
    assert wallets.doc["credits_remaining"] == 11 and len(log.rows) == 1


def test_a_refund_returns_exactly_what_a_deduction_took():
    svc, wallets, _ = _service(WALLET)
    assert _run(svc.deduct_credit("u1", "job1", reason="video_generation", amount=4)) is True
    assert wallets.doc["credits_remaining"] == 3
    _run(svc.refund_credit("u1", "job1", reason="refund", amount=4))
    assert wallets.doc["credits_remaining"] == 7
