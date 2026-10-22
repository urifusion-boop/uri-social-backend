"""
Operating modes and budget protection (PRD §4.8, §5). Pure.

advisory    URI monitors, diagnoses and recommends; the business makes every change.
approval    URI prepares a change; it runs only after someone approves it.
autonomous  URI runs pre-authorised actions itself, inside caps, with every change
            logged and notified. Needs an explicit opt-in, and the emergency stop
            halts every automated change at once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

MODES = ("advisory", "approval", "autonomous")
AUTONOMOUS_ACTIONS = ("pause_ad", "pause_campaign", "set_daily_budget")

DEFAULT_POLICY = {
    "mode": "advisory",
    # What autonomous mode may run without asking. Pausing is reversible and
    # stops spend; it is the only thing allowed by default.
    "allowed_actions": ["pause_ad"],
    "max_daily_budget_ngn": 0,          # 0 = no cap for approved changes; URI alone stays at launch budget
    "max_increase_pct_7d": 20,
    "min_spend_before_pause_ngn": 3000,
    "cooldown_hours": 48,
    "emergency_stop": False,
    "notify": True,
    "autonomous_authorized_by": "",
    "autonomous_authorized_at": None,
}


def normalize(raw: Optional[dict]) -> dict:
    raw = raw or {}
    p = dict(DEFAULT_POLICY)
    if raw.get("mode") in MODES:
        p["mode"] = raw["mode"]
    acts = raw.get("allowed_actions")
    if isinstance(acts, list):
        p["allowed_actions"] = [a for a in AUTONOMOUS_ACTIONS if a in acts]
    for k, lo, hi in (("max_daily_budget_ngn", 0, 10_000_000), ("max_increase_pct_7d", 0, 100),
                      ("min_spend_before_pause_ngn", 0, 1_000_000), ("cooldown_hours", 6, 336)):
        try:
            p[k] = max(lo, min(hi, int(raw.get(k, p[k]))))
        except (TypeError, ValueError):
            pass
    p["emergency_stop"] = bool(raw.get("emergency_stop", False))
    p["notify"] = bool(raw.get("notify", True))
    p["autonomous_authorized_by"] = str(raw.get("autonomous_authorized_by") or "")
    p["autonomous_authorized_at"] = raw.get("autonomous_authorized_at")
    # Autonomous without a recorded authorisation is advisory: the opt-in is the
    # consent, not the setting.
    if p["mode"] == "autonomous" and not p["autonomous_authorized_by"]:
        p["mode"] = "advisory"
    return p


def _aware(at) -> Optional[datetime]:
    if not isinstance(at, datetime):
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def check(policy: dict, rec: dict, state: dict, *, by_user: bool,
          at: Optional[datetime] = None) -> tuple[bool, str]:
    """May this recommendation run now? `state`: last_action_at (this campaign),
    budget_raised_7d_pct, data_fresh, blocked_by (an open tracking incident), active,
    launch_daily_ngn, wallet_ngn, markup. by_user: a person approved it just now."""
    at = at or datetime.now(timezone.utc)
    action = rec["action_type"]
    if not rec.get("executable"):
        return False, "This is a change you make yourself — mark it done when you have."
    if not state.get("data_fresh"):
        return False, "Data from Meta is out of date; refresh before changing anything."
    if state.get("blocked_by"):
        return False, f"Held while this is unresolved: {state['blocked_by']}."
    if not by_user:
        if policy["emergency_stop"]:
            return False, "Emergency stop is on — no automated changes."
        if policy["mode"] != "autonomous":
            return False, "Needs approval."
        if action not in policy["allowed_actions"]:
            return False, "Not in the actions you allowed URI to take on its own."
    last = _aware(state.get("last_action_at"))
    if last and at - last < timedelta(hours=policy["cooldown_hours"]) and action != "pause_campaign":
        return False, (f"Cooling down: the last change was under {policy['cooldown_hours']} hours ago, "
                       "so its results aren't in yet.")
    if action in ("pause_ad", "pause_campaign") and not state.get("active"):
        return False, "The campaign isn't running."
    if action == "set_daily_budget":
        to = float(rec["parameters"].get("to_ngn") or 0)
        frm = float(rec["parameters"].get("from_ngn") or 0)
        if to <= 0:
            return False, "A daily budget must be above zero."
        if to > frm:
            # An explicit cap binds everyone. Without one, a person approving may
            # raise it; URI on its own may not go above the launch budget.
            ceiling = policy["max_daily_budget_ngn"] or \
                (None if by_user else (state.get("launch_daily_ngn") or frm))
            if ceiling is not None and to > ceiling:
                if policy["max_daily_budget_ngn"]:
                    return False, (f"Your Automation settings cap daily budgets at ₦{ceiling:,.0f}. "
                                   "Raise the cap in Growth → Automation to approve this.")
                return False, (f"URI won't raise a budget above the launch budget (₦{ceiling:,.0f}) "
                               "on its own — approve it yourself, or set a cap in Growth → Automation.")
            raised = (state.get("budget_raised_7d_pct") or 0) + (to / frm - 1) * 100 if frm else 100
            if raised > policy["max_increase_pct_7d"]:
                return False, f"More than the {policy['max_increase_pct_7d']}% increase allowed in 7 days."
            need = to * float(state.get("markup") or 1)
            if float(state.get("wallet_ngn") or 0) < need:
                return False, f"Your ad wallet can't cover a day at ₦{to:,.0f}."
    return True, ""
