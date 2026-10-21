"""
Product metrics (PRD §17): "share of insights rated useful or acted on,
approval and dismissal reasons, content created from insights and first-
draft approval, false/duplicate alert rates, cost per qualified lead for
listening-informed ad tests versus baseline."

Computed only from what mi_listen_cards/mi_listen_card_actions already
record — no new tracking added for this. Two PRD-named metrics are left
out rather than invented:
- "first-draft approval" — this module has no draft-approval step of its
  own; that belongs to the separate Create Content workflow, not
  listening, and nothing here tracks it.
- "cost per qualified lead for ad tests vs baseline" — no ad-test outcome
  tracking exists anywhere in this codebase yet. There is nothing real to
  compute, so it is reported as unavailable instead of estimated.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

_USEFUL_ACTIONS = {"saved", "acted", "created"}
_CREATED_ACTIONS = {"acted", "created"}

NOT_AVAILABLE = {
    "first_draft_approval_rate": "no draft-approval tracking in this module — belongs to the separate Create Content workflow",
    "ad_test_cost_per_qualified_lead": "no ad-test outcome tracking exists yet",
}


async def product_metrics(db, market: Optional[str] = None, window_days: int = 30,
                          now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(days=window_days)

    card_q: dict = {"created_at": {"$gte": since}}
    if market:
        card_q["market"] = market
    cards_shown = await db["mi_listen_cards"].count_documents(card_q)
    if cards_shown == 0:
        return {"window_days": window_days, "cards_shown": 0, "not_available": NOT_AVAILABLE}

    action_q: dict = {"at": {"$gte": since}}
    if market:
        action_q["market"] = market
    latest_action: dict[str, dict] = {}
    async for a in db["mi_listen_card_actions"].find(action_q).sort([("at", 1)]):
        latest_action[a["card_id"]] = a  # later writes overwrite earlier ones per card, in time order

    useful = sum(1 for a in latest_action.values() if a["action"] in _USEFUL_ACTIONS)
    dismissed = [a for a in latest_action.values() if a["action"] == "dismissed"]
    created = sum(1 for a in latest_action.values() if a["action"] in _CREATED_ACTIONS)

    return {
        "window_days": window_days, "cards_shown": cards_shown,
        "useful_or_acted_on_rate": round(useful / cards_shown, 3),
        "dismissal_rate": round(len(dismissed) / cards_shown, 3),
        "dismissal_reasons": dict(Counter(a.get("reason") or "no reason given" for a in dismissed).most_common(10)),
        "content_created_count": created,
        "content_created_rate": round(created / cards_shown, 3),
        "not_available": NOT_AVAILABLE,
    }
