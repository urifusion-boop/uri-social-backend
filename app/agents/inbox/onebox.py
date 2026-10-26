"""
URI Onebox — plans, billing conversations, and the AI response gate.

URI sells a monthly allowance of AI-handled CONVERSATIONS, never messages. Two
independent limits protect the business:

  · a monthly conversation allowance (shared across every channel), and
  · a per-conversation cap on total messages (150) after which the AI stops
    replying, though people can carry on by hand.

Rules this module keeps:

  · A billing conversation is one customer's run on one channel account, ended by a
    configurable inactivity window (24h). It is separate from the platform thread, so
    one Instagram thread can hold many billing conversations over time.
  · It becomes billable once, when the FIRST substantive AI reply is accepted by the
    platform. A webhook, a failed generation, or a human-only exchange never bills.
  · Capacity is RESERVED atomically before any AI work (a message slot and, for a
    conversation not yet billed, a quota unit) and committed or released after the
    send, so concurrent jobs can't pass the cap or oversubscribe the last unit.
  · Every message counts once however often the platform redelivers it, and each
    billing is a unique ledger row, so a conversation is counted at most once per
    period.
  · Nothing here ever charges overage.

Everything tunable (plans, the cap, the window, thresholds) lives in the database and
is editable without a deploy.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

CONFIG = "inbox_onebox_config"
PLANS = "inbox_onebox_plans"
SUBSCRIPTIONS = "inbox_onebox_subscriptions"
BILLING_CONVERSATIONS = "inbox_billing_conversations"
EVENTS = "inbox_onebox_events"
LEDGER = "inbox_usage_ledger"
AI_COSTS = "inbox_ai_costs"

# Flipped off by the tests of other inbox features, which run against a fake database.
ENFORCE = True

DEFAULT_CONFIG: dict[str, Any] = {
    "message_cap": 150,
    "inactivity_hours": 24,
    "warn_at_pct": 80,
    "trial_conversations": 100,
    "trial_days": 14,
    "period_days": 30,
    "max_output_tokens": 400,
}

DEFAULT_PLANS = [
    {"plan_id": "starter", "name": "Starter", "price_ngn": 9900, "conversations": 300, "active": True},
    {"plan_id": "growth", "name": "Growth", "price_ngn": 19900, "conversations": 1000, "active": True},
    {"plan_id": "business", "name": "Business", "price_ngn": 39900, "conversations": 3000, "active": True},
]

REASONS = {
    "no_subscription": "No active Onebox plan — subscribe to turn AI replies back on",
    "quota": "Monthly AI conversation allowance used up — upgrade to keep the AI replying",
    "message_cap": "This conversation reached its {cap}-message limit, so the AI stopped replying",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at: Any) -> Optional[datetime]:
    if not isinstance(at, datetime):
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


# ── Configuration and plans (admin-editable) ─────────────────────────────────

async def get_config(db) -> dict:
    doc = await db[CONFIG].find_one({"_id": "global"}) or {}
    cfg = dict(DEFAULT_CONFIG)
    for k, default in DEFAULT_CONFIG.items():
        try:
            v = int(doc.get(k, default))
        except (TypeError, ValueError):
            v = default
        cfg[k] = max(1, v)
    return cfg


async def save_config(db, changes: dict) -> dict:
    clean = {k: max(1, int(v)) for k, v in (changes or {}).items() if k in DEFAULT_CONFIG}
    if clean:
        await db[CONFIG].update_one({"_id": "global"}, {"$set": clean}, upsert=True)
    return await get_config(db)


async def list_plans(db, include_inactive: bool = False) -> list[dict]:
    rows = await db[PLANS].find({}).to_list(50)
    if not rows:
        rows = [dict(p) for p in DEFAULT_PLANS]
    out = [{k: r.get(k) for k in ("plan_id", "name", "price_ngn", "conversations", "active")} for r in rows]
    return [p for p in out if include_inactive or p["active"]]


async def save_plan(db, plan: dict) -> dict:
    pid = str(plan.get("plan_id") or "").strip().lower()
    if not pid:
        raise ValueError("plan_id is required")
    if not await db[PLANS].count_documents({}):
        await db[PLANS].insert_many([dict(p) for p in DEFAULT_PLANS])
    fields = {"name": str(plan.get("name") or pid.title())[:40],
              "price_ngn": max(0, int(plan.get("price_ngn") or 0)),
              "conversations": max(1, int(plan.get("conversations") or 1)),
              "active": bool(plan.get("active", True))}
    await db[PLANS].update_one({"plan_id": pid}, {"$set": fields}, upsert=True)
    return {"plan_id": pid, **fields}


# ── Subscriptions ────────────────────────────────────────────────────────────

def _period_id(start: datetime) -> str:
    return start.strftime("%Y%m%d%H%M")


def _fresh(ws: str, *, kind: str, plan_id: str, name: str, allowance: int, start: datetime,
           end: datetime) -> dict:
    return {"workspace_id": ws, "status": kind, "plan_id": plan_id, "plan_name": name,
            "allowance": allowance, "used": 0, "reserved": 0, "consumed": 0,
            "period_id": _period_id(start), "period_start": start, "period_end": end,
            "queued": None, "alerted": []}


async def get_subscription(db, ws: str, create_trial: bool = True) -> Optional[dict]:
    """The workspace's current subscription, rolled forward if its period ended.

    A workspace with none gets the configured free trial the first time the inbox's AI
    is asked to act — which is how existing inbox users start on one."""
    sub = await db[SUBSCRIPTIONS].find_one({"workspace_id": ws})
    cfg = await get_config(db)
    now = _now()
    if not sub:
        if not create_trial:
            return None
        sub = _fresh(ws, kind="trial", plan_id="trial", name="Free trial",
                     allowance=cfg["trial_conversations"], start=now,
                     end=now + timedelta(days=cfg["trial_days"]))
        try:
            await db[SUBSCRIPTIONS].insert_one(dict(sub))
        except Exception:      # a concurrent first call created it
            pass
        return await db[SUBSCRIPTIONS].find_one({"workspace_id": ws})
    end = _aware(sub.get("period_end"))
    if end and now >= end and sub.get("status") != "expired":
        queued = sub.get("queued")
        if queued:
            start = end
            new = _fresh(ws, kind="active", plan_id=queued["plan_id"], name=queued["name"],
                         allowance=queued["conversations"], start=start,
                         end=start + timedelta(days=cfg["period_days"]))
            await db[SUBSCRIPTIONS].update_one(
                {"workspace_id": ws, "period_id": sub["period_id"]}, {"$set": new})
        else:
            await db[SUBSCRIPTIONS].update_one(
                {"workspace_id": ws, "period_id": sub["period_id"]}, {"$set": {"status": "expired"}})
        sub = await db[SUBSCRIPTIONS].find_one({"workspace_id": ws})
    return sub


async def activate_plan(db, ws: str, plan: dict, at: Optional[datetime] = None) -> dict:
    """A payment for `plan` succeeded. Starts now if nothing is running; otherwise it is
    queued to begin the instant the current period ends, so paying early never wastes
    or oversells the allowance."""
    at = at or _now()
    cfg = await get_config(db)
    sub = await get_subscription(db, ws, create_trial=False)
    running = (sub and sub.get("status") in ("active",) and (_aware(sub["period_end"]) or at) > at)
    if running:
        await db[SUBSCRIPTIONS].update_one(
            {"workspace_id": ws},
            {"$set": {"queued": {"plan_id": plan["plan_id"], "name": plan["name"],
                                 "conversations": plan["conversations"]}}})
    else:
        new = _fresh(ws, kind="active", plan_id=plan["plan_id"], name=plan["name"],
                     allowance=plan["conversations"], start=at,
                     end=at + timedelta(days=cfg["period_days"]))
        await db[SUBSCRIPTIONS].update_one({"workspace_id": ws}, {"$set": new}, upsert=True)
    return await db[SUBSCRIPTIONS].find_one({"workspace_id": ws})


# ── Billing conversations ────────────────────────────────────────────────────

async def _resolve_billing_conversation(db, ws: str, conv: dict, at: datetime,
                                        create: bool = True) -> Optional[dict]:
    """The billing conversation this activity belongs to: the latest one for the inbox
    conversation if it's still inside the inactivity window, else a new one."""
    cfg = await get_config(db)
    cid = str(conv["_id"])
    rows = await db[BILLING_CONVERSATIONS].find(
        {"workspace_id": ws, "conversation_id": cid}).sort("started_at", -1).limit(1).to_list(1)
    last = rows[0] if rows else None
    if last:
        seen = _aware(last.get("last_activity_at")) or _aware(last.get("started_at"))
        if seen and at - seen <= timedelta(hours=cfg["inactivity_hours"]):
            return last
        await db[BILLING_CONVERSATIONS].update_one(
            {"_id": last["_id"], "status": {"$ne": "closed"}}, {"$set": {"status": "closed"}})
    if not create:
        return None
    doc = {"workspace_id": ws, "conversation_id": cid, "platform": conv.get("platform"),
           "channel_account_id": conv.get("channel_account_id"),
           "external_thread_id": conv.get("external_thread_id"),
           "participant_id": conv.get("contact_identity_id") or conv.get("external_thread_id"),
           "started_at": at, "last_activity_at": at, "message_count": 0, "reserved": 0,
           "capacity_used": 0, "ai_replies": 0, "billed": False, "status": "open",
           "period_id": "", "human_active": False}
    res = await db[BILLING_CONVERSATIONS].insert_one(dict(doc))
    doc["_id"] = res.inserted_id
    return doc


async def _count_message(db, ws: str, bc: dict, event_id: str, *, ai: bool = False,
                         reserved: bool = False, at: Optional[datetime] = None) -> bool:
    """Count one unique message. Returns False for a repeat of the same event, which
    changes nothing. `reserved`: a slot was held for it, now filled."""
    if not event_id:
        return False
    try:
        await db[EVENTS].insert_one({"workspace_id": ws, "event_id": event_id,
                                     "billing_conversation_id": str(bc["_id"]), "at": at or _now()})
    except Exception as e:
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            if reserved:
                await db[BILLING_CONVERSATIONS].update_one(
                    {"_id": bc["_id"]}, {"$inc": {"reserved": -1, "capacity_used": -1}})
            return False
        raise
    inc: dict[str, int] = {"message_count": 1}
    if reserved:
        inc["reserved"] = -1                    # the slot turns into a counted message
    else:
        inc["capacity_used"] = 1
    if ai:
        inc["ai_replies"] = 1
    cfg = await get_config(db)
    after = await db[BILLING_CONVERSATIONS].find_one_and_update(
        {"_id": bc["_id"]}, {"$inc": inc, "$set": {"last_activity_at": at or _now()}},
        return_document=True)
    if after and after.get("capacity_used", 0) >= cfg["message_cap"] and after.get("status") == "open":
        await db[BILLING_CONVERSATIONS].update_one(
            {"_id": bc["_id"], "status": "open"},
            {"$set": {"status": "limit_reached", "limit_reached_at": _now()}})
    return True


async def record_inbound(db, ws: str, conv: dict, event_id: str,
                         at: Optional[datetime] = None) -> None:
    """Every stored customer message counts toward its billing conversation, whether or
    not the AI ever replies. Never raises: counting must not cost a customer message."""
    if not ENFORCE:
        return
    try:
        at = at or _now()
        bc = await _resolve_billing_conversation(db, ws, conv, at)
        await _count_message(db, ws, bc, event_id, at=at)
    except Exception as e:
        print(f"[Onebox] couldn't count an inbound message: {e}", flush=True)


async def record_outbound(db, ws: str, conv: dict, event_id: str, actor_id: str) -> None:
    """A customer-visible message from a person (or from the AI's holding line). The
    AI's own substantive replies are counted through the reservation, not here."""
    if not ENFORCE:
        return
    try:
        bc = await _resolve_billing_conversation(db, ws, conv, _now())
        await _count_message(db, ws, bc, event_id)
        from .agent import AGENT_ACTOR
        if actor_id != AGENT_ACTOR:
            await db[BILLING_CONVERSATIONS].update_one({"_id": bc["_id"]}, {"$set": {"human_active": True}})
    except Exception as e:
        print(f"[Onebox] couldn't count an outbound message: {e}", flush=True)


# ── The AI response gate ─────────────────────────────────────────────────────

@dataclass
class Decision:
    allowed: bool
    code: str = ""
    reason: str = ""
    reservation: Optional[dict] = None


async def authorize(db, ws: str, conv: dict, *, reserve: bool) -> Decision:
    """May the AI reply to this conversation right now?

    reserve=False is the cheap check before spending anything on the model. reserve=True
    atomically holds a message slot (and a quota unit if this conversation isn't billed
    yet) for the reply about to be sent."""
    if not ENFORCE:
        return Decision(True)
    cfg = await get_config(db)
    sub = await get_subscription(db, ws)
    if not sub or sub.get("status") not in ("trial", "active"):
        return Decision(False, "no_subscription", REASONS["no_subscription"])
    now = _now()
    bc = await _resolve_billing_conversation(db, ws, conv, now)
    needs_unit = not bc.get("billed")

    if bc.get("capacity_used", 0) >= cfg["message_cap"] or bc.get("status") == "limit_reached":
        return Decision(False, "message_cap", REASONS["message_cap"].format(cap=cfg["message_cap"]))
    if needs_unit and sub.get("consumed", 0) >= sub.get("allowance", 0):
        return Decision(False, "quota", REASONS["quota"])
    if not reserve:
        return Decision(True)

    slot = await db[BILLING_CONVERSATIONS].find_one_and_update(
        {"_id": bc["_id"], "capacity_used": {"$lt": cfg["message_cap"]}},
        {"$inc": {"reserved": 1, "capacity_used": 1}}, return_document=True)
    if not slot:
        return Decision(False, "message_cap", REASONS["message_cap"].format(cap=cfg["message_cap"]))
    res = {"bc_id": str(bc["_id"]), "quota": False, "period_id": sub["period_id"]}
    if needs_unit:
        unit = await db[SUBSCRIPTIONS].find_one_and_update(
            {"workspace_id": ws, "period_id": sub["period_id"],
             "consumed": {"$lt": sub["allowance"]}},
            {"$inc": {"reserved": 1, "consumed": 1}}, return_document=True)
        if not unit:
            await db[BILLING_CONVERSATIONS].update_one(
                {"_id": bc["_id"]}, {"$inc": {"reserved": -1, "capacity_used": -1}})
            return Decision(False, "quota", REASONS["quota"])
        res["quota"] = True
    return Decision(True, reservation=res)


async def _bc_by_id(db, bc_id: str) -> Optional[dict]:
    from bson import ObjectId
    return await db[BILLING_CONVERSATIONS].find_one({"_id": ObjectId(bc_id)})


async def release(db, ws: str, res: Optional[dict]) -> None:
    """The reply wasn't sent: hand the reserved capacity back."""
    if not res or not ENFORCE:
        return
    from bson import ObjectId
    await db[BILLING_CONVERSATIONS].update_one(
        {"_id": ObjectId(res["bc_id"])}, {"$inc": {"reserved": -1, "capacity_used": -1}})
    if res.get("quota"):
        await db[SUBSCRIPTIONS].update_one(
            {"workspace_id": ws, "period_id": res["period_id"]},
            {"$inc": {"reserved": -1, "consumed": -1}})


async def commit(db, ws: str, res: Optional[dict], provider_message_id: str) -> None:
    """The platform accepted the AI's reply: count the message and, the first time for
    this conversation, the billable unit."""
    if not res or not ENFORCE:
        return
    bc = await _bc_by_id(db, res["bc_id"])
    if not bc:
        return
    await _count_message(db, ws, bc, provider_message_id, ai=True, reserved=True)
    if not res.get("quota"):
        return
    try:
        await db[LEDGER].insert_one({
            "workspace_id": ws, "period_id": res["period_id"], "billing_conversation_id": res["bc_id"],
            "platform": bc.get("platform"), "reason": "first AI reply accepted", "at": _now()})
    except Exception as e:
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            # Another job already billed this conversation: give its unit back.
            await db[SUBSCRIPTIONS].update_one(
                {"workspace_id": ws, "period_id": res["period_id"]},
                {"$inc": {"reserved": -1, "consumed": -1}})
            return
        raise
    await db[SUBSCRIPTIONS].update_one(
        {"workspace_id": ws, "period_id": res["period_id"]}, {"$inc": {"reserved": -1, "used": 1}})
    await db[BILLING_CONVERSATIONS].update_one(
        {"_id": bc["_id"]}, {"$set": {"billed": True, "period_id": res["period_id"]}})


# ── Usage, warnings ──────────────────────────────────────────────────────────

async def usage(db, ws: str) -> dict:
    """What the workspace dashboard shows."""
    cfg = await get_config(db)
    sub = await get_subscription(db, ws)
    allowance, used = int(sub.get("allowance", 0)), int(sub.get("used", 0))
    pct = round(used / allowance * 100) if allowance else 0
    convs = await db[BILLING_CONVERSATIONS].find(
        {"workspace_id": ws, "started_at": {"$gte": _aware(sub["period_start"])}}).to_list(5000)
    counts = sorted(c.get("message_count", 0) for c in convs)
    return {
        "plan_id": sub.get("plan_id"), "plan_name": sub.get("plan_name"), "status": sub.get("status"),
        "allowance": allowance, "used": used, "remaining": max(0, allowance - used),
        "percent_used": pct, "period_start": sub.get("period_start"), "period_end": sub.get("period_end"),
        "warning": cfg["warn_at_pct"] <= pct < 100, "exhausted": allowance > 0 and used >= allowance,
        "message_cap": cfg["message_cap"], "queued_plan": (sub.get("queued") or {}).get("name"),
        "conversations": len(convs),
        "at_message_cap": sum(1 for c in convs if c.get("status") == "limit_reached"),
        "median_messages": counts[len(counts) // 2] if counts else 0,
        "average_messages": round(sum(counts) / len(counts), 1) if counts else 0,
    }


async def threshold_crossed(db, ws: str) -> Optional[str]:
    """'warning' or 'exhausted' once per period each, so an alert goes out once."""
    cfg = await get_config(db)
    sub = await get_subscription(db, ws, create_trial=False)
    if not sub or not sub.get("allowance"):
        return None
    pct = sub["used"] / sub["allowance"] * 100
    for name, hit in (("exhausted", pct >= 100), ("warning", pct >= cfg["warn_at_pct"])):
        if hit and name not in (sub.get("alerted") or []):
            claimed = await db[SUBSCRIPTIONS].find_one_and_update(
                {"workspace_id": ws, "period_id": sub["period_id"], "alerted": {"$ne": name}},
                {"$push": {"alerted": name}}, return_document=True)
            if claimed:
                return name
    return None


# ── AI cost tracking ─────────────────────────────────────────────────────────

_usage: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("onebox_usage", default=None)


def start_usage() -> contextvars.Token:
    return _usage.set([])


def note_usage(model: str, usage_obj: Any) -> None:
    bucket = _usage.get()
    if bucket is None or usage_obj is None:
        return
    bucket.append({"model": model, "input": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                   "output": int(getattr(usage_obj, "completion_tokens", 0) or 0)})


# USD per million tokens, input / output. An estimate for tracking, not a bill.
_PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.5, 10.0)}


async def record_cost(db, ws: str, conversation_id: str, token: contextvars.Token) -> None:
    bucket = _usage.get() or []
    _usage.reset(token)
    if not bucket or not ENFORCE:
        return
    inp, out = sum(b["input"] for b in bucket), sum(b["output"] for b in bucket)
    model = bucket[-1]["model"]
    pin, pout = next((v for k, v in _PRICES.items() if model.startswith(k)), (0.0, 0.0))
    try:
        await db[AI_COSTS].insert_one({
            "workspace_id": ws, "conversation_id": conversation_id, "model": model, "calls": len(bucket),
            "input_tokens": inp, "output_tokens": out,
            "estimated_cost_usd": round((inp * pin + out * pout) / 1_000_000, 6), "at": _now()})
    except Exception as e:
        print(f"[Onebox] couldn't record AI cost: {e}", flush=True)
