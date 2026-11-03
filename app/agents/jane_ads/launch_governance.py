"""
Jane + Ads — who may launch, what they approved, and what happened to it
(Ads Intelligence PRD §7.8 PB-04..06, §13).

Launching spends a client's money, so it is the one step that must be exactly once,
by someone allowed to, against a configuration that is on record:

· **Role.** In an agency brand only an agency admin launches, turns a campaign on or
  deletes it; an agent prepares plans. A solo workspace's owner always may.
· **Exactly once.** The plan is claimed atomically (pending → launching) before Meta is
  called, so a double-click or a retried request finds it already claimed instead of
  creating a second campaign. A launch that fails before Meta has a campaign hands the
  plan back (→ pending); once Meta has one, the plan never goes back.
· **Approval on record.** The approver, the time and a hash of the exact configuration
  submitted are written once and never updated.
· **State history.** Every transition is appended with who and when.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

PLANS = "jane_ads_pending_plans"
APPROVALS = "jane_ads_launch_approvals"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def can_launch(db, brand_ctx: dict) -> bool:
    if not brand_ctx.get("agency_id"):
        return True
    try:
        from app.services.AgencyService import AgencyService
        return bool(await AgencyService.is_agency_admin(brand_ctx.get("user_id", ""), brand_ctx["agency_id"], db))
    except Exception:
        return False


async def require_launch_role(db, brand_ctx: dict, what: str = "launch a campaign") -> None:
    if not await can_launch(db, brand_ctx):
        raise HTTPException(status_code=403, detail=f"Only an agency admin can {what} for this brand.")


def config_hash(plan: dict, req: dict) -> str:
    """A fingerprint of what was approved: any later difference shows up as a different hash."""
    blob = json.dumps({"plan": plan, "req": req}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def claim(db, plan_id: str, actor: str) -> Optional[dict]:
    """pending → launching, atomically. None when someone else already holds it."""
    from pymongo import ReturnDocument
    now = _now()
    return await db[PLANS].find_one_and_update(
        {"plan_id": plan_id, "status": "pending"},
        {"$set": {"status": "launching", "launching_at": now, "launching_by": actor},
         "$push": {"state_history": {"state": "approved", "at": now, "actor": actor}}},
        return_document=ReturnDocument.AFTER,
    )


async def record_approval(db, doc: dict, actor: str, due_ngn: float) -> str:
    """The immutable approval: who approved which configuration, for how much."""
    h = config_hash(doc.get("plan") or {}, doc.get("req") or {})
    await db[APPROVALS].insert_one({
        "plan_id": doc["plan_id"], "brand_id": doc.get("brand_id"), "business_id": doc.get("business_id"),
        "approved_by": actor, "approved_at": _now(), "config_hash": h,
        "total_due_ngn": due_ngn, "plan": doc.get("plan"), "req": doc.get("req"),
    })
    return h


async def release(db, plan_id: str, actor: str, error: str) -> None:
    """Meta has no campaign: give the plan back so it can be fixed and launched again."""
    await db[PLANS].update_one(
        {"plan_id": plan_id, "status": "launching"},
        {"$set": {"status": "pending", "last_launch_error": error[:500]},
         "$push": {"state_history": {"state": "failed", "at": _now(), "actor": actor, "error": error[:500]}}},
    )


async def mark_launched(db, plan_id: str, actor: str, campaign_id: str, **extra) -> None:
    await db[PLANS].update_one(
        {"plan_id": plan_id},
        {"$set": {"status": "launched", "campaign_id": campaign_id, **extra},
         "$push": {"state_history": {"state": "submitted", "at": _now(), "actor": actor,
                                     "campaign_id": campaign_id}}},
    )


async def audit(db, brand_id: str, campaign_id: str, kind: str, actor: str, **data) -> None:
    """One trail for every consequential step, alongside Growth's own events."""
    from .growth.engine import event
    try:
        await event(db, brand_id, campaign_id, kind, actor=actor, **data)
    except Exception as e:  # an audit write never costs the client the action itself
        print(f"[audit] {kind} for {campaign_id or '-'} not recorded: {e}", flush=True)
