"""
Post-change evaluation and learning (PRD §4.5, §12, Phases 3–4).

After an action's window, compare the same length of time before and after it on
the metric the recommendation named. The verdict is "improved", "worse" or
"inconclusive" — inconclusive whenever either side is too small to say — and it is
always an association: the auction, the season and the audience moved too.

Across a brand's history, those verdicts become a plain-language track record per
action ("pausing a weak ad lowered cost per conversation in 2 of 3 past cases"),
shown beside new recommendations. It informs; it never decides.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .actions import ACTIONS
from .stats import totals

MIN_CONVERSATIONS_EACH_SIDE = 5


def compare(before: dict, after: dict, action: str) -> dict:
    lower_is_better = action in ("pause_ad", "set_daily_budget", "refresh_creative", "review_targeting")
    b, a = before.get("cost_per_conversation_ngn"), after.get("cost_per_conversation_ngn")
    if action == "pause_campaign":
        return {"verdict": "improved" if after["spend_ngn"] < before["spend_ngn"] else "inconclusive",
                "metric": "spend_ngn", "before": round(before["spend_ngn"], 2),
                "after": round(after["spend_ngn"], 2),
                "note": "Spend stopped while paused; results after resuming show whether the fix worked."}
    if min(before["conversations"], after["conversations"]) < MIN_CONVERSATIONS_EACH_SIDE or not (a and b):
        return {"verdict": "inconclusive", "metric": "cost_per_conversation_ngn",
                "before": round(b, 2) if b else None, "after": round(a, 2) if a else None,
                "note": "Too few conversations on one side to tell."}
    change = a / b - 1
    if abs(change) < 0.1:
        verdict = "inconclusive"
    else:
        verdict = "improved" if (change < 0) == lower_is_better else "worse"
    return {"verdict": verdict, "metric": "cost_per_conversation_ngn", "before": round(b, 2),
            "after": round(a, 2), "change": round(change, 3),
            "note": "Associated with the change, not proof it caused it."}


async def evaluate_due(db, snapshots_for) -> int:
    """Evaluate executed actions whose window has passed. snapshots_for(campaign_id)
    returns that campaign's daily rows."""
    now = datetime.now(timezone.utc)
    done = 0
    async for a in db[ACTIONS].find({"status": "executed", "evaluation": {"$exists": False},
                                     "evaluate_after": {"$lte": now}}):
        rows = await snapshots_for(a["campaign_id"])
        at = a["executed_at"].date() if isinstance(a.get("executed_at"), datetime) else now.date()
        days = max(1, (a["evaluate_after"] - a["executed_at"]).days) if isinstance(a.get("evaluate_after"), datetime) else 3
        before_d = {(at - timedelta(days=i)).isoformat() for i in range(1, days + 1)}
        after_d = {(at + timedelta(days=i)).isoformat() for i in range(1, days + 1)}
        before = totals([r for r in rows if r.get("date") in before_d])
        after = totals([r for r in rows if r.get("date") in after_d])
        result = compare(before, after, a["action_type"])
        await db[ACTIONS].update_one({"_id": a["_id"]}, {"$set": {"evaluation": {
            **result, "evaluated_at": now, "window_days": days}}})
        done += 1
    return done


async def track_record(db, brand_id: str) -> dict:
    """{action_type: {"improved", "worse", "inconclusive", "text"}} for this brand."""
    out: dict[str, dict] = {}
    async for a in db[ACTIONS].find({"brand_id": brand_id, "evaluation": {"$exists": True}}):
        r = out.setdefault(a["action_type"], {"improved": 0, "worse": 0, "inconclusive": 0})
        r[a["evaluation"]["verdict"]] = r.get(a["evaluation"]["verdict"], 0) + 1
    for act, r in out.items():
        n = r["improved"] + r["worse"] + r["inconclusive"]
        r["text"] = (f"Past results for this kind of change on your campaigns: improved in {r['improved']} "
                     f"of {n}, worse in {r['worse']}, unclear in {r['inconclusive']}.")
    return out
