"""
Jane + Ads — keeping a campaign running past the date it was set to end.

A campaign is launched for a fixed number of days and Meta stops delivering when the
ad set's end_time passes. Until now the only way onward was a brand-new campaign: a
new plan, a new creative and a fresh learning phase. For a campaign that is WORKING
that is the worst possible moment to start over — it throws away everything Meta has
learned about who responds, which is the most valuable thing a running ad accumulates.

Extending keeps the campaign id, the ad set, the creative and the learning. All that
moves is the end date.

Two things this must get right, both about money:

· **Quote before charging.** Extending costs days × the daily spend, and the client
  sees that figure before anything is debited or changed.

· **Charge only after Meta has actually extended.** A launch debits after the campaign
  exists for exactly this reason: a client must never pay for delivery they did not
  get. The wallet is checked first so the common failure is a refusal, not a
  half-finished extension.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from . import constants as C

# The same bounds a fresh plan gets. An extension is not a loophole around them.
MIN_EXTEND_DAYS = 1
MAX_EXTEND_DAYS = 30


class ExtendError(Exception):
    """A refusal the client should read — never a surprise at the wallet."""


def quote(daily_ngn: float, days: int, markup: float) -> dict:
    """What extending will cost, before anything changes.

    `markup` is the rate this campaign was SOLD under, carried on its own record, so a
    later change to Uri's fee never re-bases a campaign onto maths its owner never
    agreed to — the same rule billing.py follows.
    """
    if days < MIN_EXTEND_DAYS or days > MAX_EXTEND_DAYS:
        raise ExtendError(
            f"Choose between {MIN_EXTEND_DAYS} and {MAX_EXTEND_DAYS} more days."
        )
    if daily_ngn < C.MIN_DAILY_SPEND_NGN:
        raise ExtendError(
            f"This campaign spends ₦{daily_ngn:,.0f} a day, under the "
            f"₦{C.MIN_DAILY_SPEND_NGN:,.0f} minimum — it cannot be extended as it is."
        )
    ad_spend = round(daily_ngn * days, 2)
    return {
        "days": days,
        "daily_ngn": daily_ngn,
        "ad_spend_ngn": ad_spend,
        "total_due_ngn": round(ad_spend * markup, 2),
    }


def new_end_time(current_end: Optional[str], days: int, now: Optional[datetime] = None) -> datetime:
    """Where the campaign should now end.

    Measured from whichever is LATER — its current end date or now. Extending a
    campaign that ended a week ago by three days must give three days of delivery from
    today, not three days that already elapsed.
    """
    now = now or datetime.now(timezone.utc)
    base = now
    if current_end:
        try:
            parsed = datetime.fromisoformat(current_end.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            base = max(parsed, now)
        except ValueError:
            pass
    return base + timedelta(days=days)
