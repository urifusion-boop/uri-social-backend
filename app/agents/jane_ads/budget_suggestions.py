"""
Suggested budgets for the campaign builder's budget step (PRD WS5: "select a recommended budget
or enter a custom amount").

Worked out from the same rules the planner enforces, so a suggestion is always one that plans and
launches: each Meta ad needs Jane's daily floor (MIN_DAILY_SPEND_NGN) with some headroom, TikTok's
ad group needs its own daily floor, campaigns run at most MAX_CAMPAIGN_DAYS, and URI's fee comes
out of the stated budget. Each option says what it buys in plain words.
"""
from __future__ import annotations

import math

from . import constants as C

# Each Meta ad gets 1.5x the floor a day, so a test isn't sitting right on the minimum.
META_PER_AD_DAILY = C.MIN_DAILY_SPEND_NGN * 1.5
GOOGLE_DAILY_TIERS = (1_500.0, 3_500.0, 7_000.0)


def _round_up(n: float, step: int = 1_000) -> int:
    return int(math.ceil(n / step) * step)


def _stated(ad_spend: float, fee: bool) -> int:
    return _round_up(C.budget_for_ad_spend(ad_spend) if fee else ad_spend)


def suggest(platform: str = "meta", days: int = 7) -> dict:
    platform = (platform or "meta").lower()
    asked = max(1, int(days or 7))
    run = min(asked, C.MAX_CAMPAIGN_DAYS)
    note = (f"Campaigns run up to {C.MAX_CAMPAIGN_DAYS} days at a time; 'Keep it running' extends one "
            "that's working.") if asked > run else ""

    if platform == "tiktok":
        floor = C.HARD_FLOOR_DAILY_NGN.get("tiktok", 31_000.0)
        # Fewest days that clear Jane's useful minimum for TikTok, or she'd advise against it.
        least = min(run, max(1, math.ceil(C.USEFUL_MIN_NGN["tiktok"] / floor)))
        tiers = [(least, "Try it"), (max(least, min(3, run)), "Recommended"), (run, "Full week")]
        seen, options = set(), []
        for d, label in tiers:
            if d in seen:
                continue
            seen.add(d)
            amount = _stated(floor * d, fee=False)  # TikTok's fee is waived for now (router: fee_bypassed)
            options.append({"amount_ngn": amount, "label": label, "days": d, "ads": 1,
                            "what": f"{d} day{'s' if d != 1 else ''} on TikTok",
                            "detail": f"TikTok needs about ₦{floor:,.0f} a day for an ad to run."})
        rec = min(1, len(options) - 1)
        for i, o in enumerate(options):
            o["recommended"] = i == rec
        return {"platform": "tiktok", "days": run, "options": options, "note": note}

    if platform == "google":
        options = []
        for i, (daily, label) in enumerate(zip(GOOGLE_DAILY_TIERS, ("Start small", "Recommended", "More reach"))):
            options.append({"amount_ngn": _stated(daily * run, fee=True), "label": label, "days": run, "ads": 1,
                            "what": f"about ₦{daily:,.0f} a day in searches · {run} days",
                            "detail": "You pay when someone taps your ad, so more budget means more taps.",
                            "recommended": i == 1})
        return {"platform": "google", "days": run, "options": options, "note": note}

    options = []
    for n, label in ((1, "Start small"), (2, "Recommended"), (3, "Test more ideas")):
        daily = META_PER_AD_DAILY * n
        amount = _stated(daily * run, fee=True)
        what = f"1 ad · {run} days" if n == 1 else f"Tests {n} ads · {run} days"
        options.append({"amount_ngn": amount, "label": label, "days": run, "ads": n, "what": what,
                        "detail": (f"About ₦{daily:,.0f} a day goes to ads"
                                   + (f", shared by {n} ads so Meta can tell which works." if n > 1 else ".")),
                        "recommended": n == 2})
    return {"platform": "meta", "days": run, "options": options, "note": note}
