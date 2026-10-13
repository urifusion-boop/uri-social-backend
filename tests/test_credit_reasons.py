"""Every reason a credit is charged for must be loggable.

The wallet is decremented first and the transaction logged second, so a reason
the log model rejects turns a successful charge into a 500 — the user pays and
gets nothing back. Seen on dev: blog generation ("blog_generation").
"""
import re
from pathlib import Path
from typing import get_args

import pytest

from app.domain.models.billing_models import CreditTransaction

APP = Path(__file__).resolve().parents[1] / "app"
ALLOWED = set(get_args(CreditTransaction.model_fields["reason"].annotation))
CHARGE_CALL = re.compile(
    r"(?:deduct_credit|deduct_trial_credit|_check_and_deduct_credit|deduct_for_brand|refund_credit|refund_trial_credit)\("
    r"(?P<args>[^()]*(?:\([^()]*\)[^()]*)*)\)", re.S)
REASON = re.compile(r"(?:reason|operation)\s*=\s*[\"']([a-z_]+)[\"']")


def charged_reasons():
    found = {}
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for call in CHARGE_CALL.finditer(text):
            for reason in REASON.findall(call.group("args")):
                found.setdefault(reason, path.name)
    return found


def test_finds_the_known_charges():
    assert {"campaign_generation", "blog_generation", "image_retry"} <= set(charged_reasons())


@pytest.mark.parametrize("reason,where", sorted(charged_reasons().items()))
def test_reason_can_be_logged(reason, where):
    assert reason in ALLOWED, f"{where} charges credits for '{reason}', which CreditTransaction rejects"
    CreditTransaction(user_id="u", type="deduction", amount=-1, balance_before=2, balance_after=1, reason=reason)
