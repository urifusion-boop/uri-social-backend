"""
Forecast ranges (PRD Phase 4). A range for the rest of the flight from the
campaign's own daily variation — an estimate with its assumptions spelled out,
never a promise.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from .stats import mean_sd

MIN_DAYS = 4
Z80 = 1.2816


def project(rows: list[dict], ends_at: Optional[datetime], today: Optional[date] = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    daily: dict[str, dict] = {}
    for r in rows:
        if r.get("date") and r["date"] < today.isoformat():
            d = daily.setdefault(r["date"], {"spend": 0.0, "conv": 0.0})
            d["spend"] += float(r.get("spend_ngn") or 0)
            d["conv"] += float(r.get("conversations") or 0)
    if len(daily) < MIN_DAYS or not ends_at:
        return {"available": False,
                "reason": f"Needs at least {MIN_DAYS} full days of delivery and an end date."}
    remaining = max(0, (ends_at.date() - today).days)
    days = sorted(daily)[-7:]
    conv_m, conv_sd = mean_sd([daily[d]["conv"] for d in days])
    spend_m, _ = mean_sd([daily[d]["spend"] for d in days])
    spread = Z80 * conv_sd * (remaining ** 0.5)
    low, high = max(0.0, conv_m * remaining - spread), conv_m * remaining + spread
    return {
        "available": True,
        "remaining_days": remaining,
        "conversations_range": [int(low), int(round(high))],
        "spend_estimate_ngn": round(spend_m * remaining, 0),
        "assumptions": [f"Based on the last {len(days)} days of delivery.",
                        "Assumes budget, targeting and creative stay as they are.",
                        "An 80% range: results outside it are possible."],
    }
