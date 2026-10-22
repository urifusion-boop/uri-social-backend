"""
The action executor (PRD §4.8, §6). Every change:

  1. passes policy.check (mode, caps, cooldown, freshness, open incidents);
  2. runs once per recommendation — the recommendation id is the idempotency key,
     so a retried request returns the first result instead of acting twice;
  3. records the state before, the change asked for, Meta's answer, and the state
     read back after (Meta answering 200 means accepted, not applied);
  4. is notified, and waits in the log for its evaluation window.

Mutations are never retried blindly: a failure is recorded and surfaced.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

ACTIONS = "jane_ads_growth_actions"


async def _read(adapter, node_id: str, fields: str) -> dict:
    if getattr(adapter, "demo", False):
        return await adapter.read(node_id, fields)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{adapter._graph_base}/{node_id}",
                                params={"access_token": adapter._access_token, "fields": fields})
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message") or "read failed")
    return data


async def _post(adapter, node_id: str, body: dict) -> dict:
    if getattr(adapter, "demo", False):
        return await adapter.post(node_id, body)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{adapter._graph_base}/{node_id}",
                                 params={"access_token": adapter._access_token}, json=body)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message") or "update failed")
    return data


async def state_of(adapter, record: dict, action: str, params: dict) -> dict:
    if action in ("pause_ad", "resume_ad"):
        ad = await _read(adapter, params.get("ad_id") or record.get("ad_id"), "id,status,effective_status")
        return {"ad_id": ad.get("id"), "status": ad.get("status"), "effective_status": ad.get("effective_status")}
    sched = await adapter.fetch_adset_schedule(record["campaign_id"])
    camp = await _read(adapter, record["campaign_id"], "id,status,effective_status")
    return {"campaign_status": camp.get("status"), "effective_status": camp.get("effective_status"),
            "adset_status": sched.get("status"), "daily_budget_ngn": sched.get("daily_ngn")}


async def _mutate(adapter, record: dict, action: str, params: dict) -> dict:
    if action == "pause_ad":
        return await _post(adapter, params.get("ad_id") or record["ad_id"], {"status": "PAUSED"})
    if action == "resume_ad":
        return await _post(adapter, params.get("ad_id") or record["ad_id"], {"status": "ACTIVE"})
    if action == "pause_campaign":
        return await adapter.set_delivery(record["campaign_id"], False)
    if action == "resume_campaign":
        return await adapter.set_delivery(record["campaign_id"], True)
    if action == "set_daily_budget":
        kobo = int(round(float(params["to_ngn"]) * 100))
        return await _post(adapter, record["adset_id"], {"daily_budget": kobo})
    raise ValueError(f"not an executable action: {action}")


def verified(action: str, params: dict, after: dict) -> bool:
    if action == "pause_ad":
        return after.get("status") == "PAUSED"
    if action == "resume_ad":
        return after.get("status") == "ACTIVE"
    if action == "pause_campaign":
        return after.get("campaign_status") == "PAUSED"
    if action == "resume_campaign":
        return after.get("campaign_status") == "ACTIVE"
    if action == "set_daily_budget":
        return abs(float(after.get("daily_budget_ngn") or 0) - float(params["to_ngn"])) < 1
    return False


async def execute(db, adapter, record: dict, rec: dict, *, actor: str, mode: str) -> dict:
    """Run one approved/authorised recommendation. Returns the action log entry."""
    key = rec["id"]
    prior = await db[ACTIONS].find_one({"idempotency_key": key}, {"_id": 0})
    if prior and prior.get("status") in ("executed", "running"):
        return prior

    now = datetime.now(timezone.utc)
    action, params = rec["action_type"], dict(rec.get("parameters") or {})
    entry = {
        "action_id": uuid.uuid4().hex, "idempotency_key": key,
        "brand_id": record.get("brand_id"), "campaign_id": record["campaign_id"],
        "recommendation_id": rec["id"], "diagnosis_id": rec.get("diagnosis_id"),
        "action_type": action, "parameters": params, "actor": actor, "mode": mode,
        "requested_change": rec.get("summary"), "status": "running", "started_at": now,
    }
    await db[ACTIONS].update_one({"idempotency_key": key}, {"$set": entry}, upsert=True)
    try:
        entry["before_state"] = await state_of(adapter, record, action, params)
        entry["api_result"] = await _mutate(adapter, record, action, params)
        entry["after_state"] = await state_of(adapter, record, action, params)
        entry["verified"] = verified(action, params, entry["after_state"])
        entry["status"] = "executed" if entry["verified"] else "unverified"
    except Exception as e:
        entry["status"] = "failed"
        entry["error"] = str(e)[:300]
    entry["executed_at"] = datetime.now(timezone.utc)
    entry["evaluate_after"] = entry["executed_at"] + timedelta(days=int(rec.get("eval_window_days") or 3))
    await db[ACTIONS].update_one({"idempotency_key": key}, {"$set": entry})
    return {k: v for k, v in entry.items() if k != "_id"}


async def last_action_at(db, campaign_id: str) -> Optional[datetime]:
    row = await db[ACTIONS].find({"campaign_id": campaign_id, "status": {"$in": ["executed", "unverified"]}}) \
        .sort("executed_at", -1).limit(1).to_list(1)
    return row[0]["executed_at"] if row else None


async def budget_raised_pct_7d(db, campaign_id: str) -> float:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    total = 0.0
    async for a in db[ACTIONS].find({"campaign_id": campaign_id, "action_type": "set_daily_budget",
                                     "status": "executed", "executed_at": {"$gte": since}}):
        frm = float((a.get("parameters") or {}).get("from_ngn") or 0)
        to = float((a.get("parameters") or {}).get("to_ngn") or 0)
        if frm and to > frm:
            total += (to / frm - 1) * 100
    return total


async def list_ads(adapter, record: dict) -> list[dict]:
    """The campaign's ads as Meta holds them now: id, name, status."""
    if getattr(adapter, "demo", False):
        return [{"id": a, "name": a.rsplit("-", 1)[-1].replace("-", " ").title(), "status": st}
                for a, st in (record.get("ads") or {}).items()]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{adapter._graph_base}/{record['campaign_id']}/ads",
                                params={"access_token": adapter._access_token,
                                        "fields": "id,name,status,effective_status", "limit": 50})
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message") or "couldn't list ads")
    return [{"id": a["id"], "name": a.get("name") or a["id"], "status": a.get("status"),
             "effective_status": a.get("effective_status")} for a in data.get("data") or []]
