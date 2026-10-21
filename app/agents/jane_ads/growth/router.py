"""
Growth Intelligence endpoints (PRD §8). Everything is scoped to the caller's active
brand: a campaign id from another brand is a 404, never a peek.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency


def _jwt():
    from app.core.auth_bearer import JWTBearer
    return JWTBearer()

from . import actions as act
from . import creative, engine, evaluate, forecast, leads, metrics
from . import policy as pol
from .detect import totals_for_ui

router = APIRouter(prefix="/jane-ads/growth", tags=["Jane + Ads — Growth Intelligence"])


def _brand(ctx: dict) -> str:
    return ctx.get("brand_id") or ""


async def _can_change(db, ctx: dict) -> bool:
    """Policy and automation are admin decisions: a solo workspace's owner, or an
    agency admin for an agency brand."""
    if not ctx.get("agency_id"):
        return True
    try:
        from app.services.AgencyService import AgencyService
        return bool(await AgencyService.is_agency_admin(ctx.get("user_id", ""), ctx["agency_id"], db))
    except Exception:
        return False


async def _campaign(db, brand: str, campaign_id: str) -> dict:
    record = await db[engine.CAMPAIGNS].find_one({"campaign_id": campaign_id})
    if not record or record.get("brand_id") != brand:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return record


def _adapter(db):
    adapter = engine.adapter_for(db)
    if adapter is None:
        raise HTTPException(status_code=400, detail="Meta ads aren't configured")
    return adapter


def _with_lifetime(t: dict, health: dict) -> dict:
    """Meta's lifetime totals when we have them: the daily rows can leave spend out."""
    life = health.get("lifetime")
    if not life:
        return t
    from .stats import derive
    out = derive({**life, "days": t.get("days", 0)})
    out["daily_complete"] = health.get("daily_complete", True)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def _clean(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "_id"}


@router.get("/overview")
async def overview(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Campaign Health for every campaign in the brand, plus what needs attention."""
    brand = _brand(ctx)
    out = []
    async for r in db[engine.CAMPAIGNS].find({"brand_id": brand}).sort("created_at", -1).limit(50):
        cid = r["campaign_id"]
        rows = await metrics.load(db, cid)
        h = await metrics.health(db, cid)
        goal = await engine.load_goal(db, cid)
        t = _with_lifetime(totals_for_ui(rows), h)
        open_count = await db[engine.DIAGNOSES].count_documents({"campaign_id": cid, "status": "open"})
        out.append({
            "campaign_id": cid, "name": r.get("display_name") or "Campaign",
            "budget_ngn": r.get("budget_ngn"), "ended": bool(r.get("ended_notified")),
            "totals": t, "goal": goal,
            "result_vs_target": (round(t["cost_per_conversation_ngn"] / goal["target_cpl_ngn"], 2)
                                 if goal.get("target_cpl_ngn") and t.get("cost_per_conversation_ngn") else None),
            "budget_used": (round(t["spend_ngn"] / float(r["budget_ngn"]), 3) if r.get("budget_ngn") else None),
            "last_updated": h.get("last_success_at"), "last_error": h.get("last_error") or "",
            "open_issues": open_count,
        })
    policy = await engine.load_policy(db, brand)
    pending = await db[engine.RECOMMENDATIONS].count_documents({"brand_id": brand, "status": "proposed"})
    return {"campaigns": out, "policy": policy, "pending_recommendations": pending,
            "can_change_policy": await _can_change(db, ctx)}


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                          ctx: dict = Depends(get_active_brand_context)) -> dict:
    brand = _brand(ctx)
    r = await _campaign(db, brand, campaign_id)
    rows = await metrics.load(db, campaign_id)
    ad_rows = await metrics.load(db, campaign_id, level="ad")
    h = await metrics.health(db, campaign_id)
    ad_ids = sorted({x["ad_id"] for x in ad_rows if x.get("ad_id")} | {r.get("ad_id") or ""} - {""})
    t = _with_lifetime(totals_for_ui(rows), h)
    diagnoses = [_clean(d) for d in await db[engine.DIAGNOSES].find(
        {"campaign_id": campaign_id}).sort("created_at", -1).limit(30).to_list(30)]
    recs = [_clean(x) for x in await db[engine.RECOMMENDATIONS].find(
        {"campaign_id": campaign_id}).sort("created_at", -1).limit(60).to_list(60)]
    variants = await db[creative.VARIANTS].find({"campaign_id": campaign_id}, {"_id": 0}).to_list(50)
    daily: dict[str, dict] = {}
    for x in rows:
        d = daily.setdefault(x["date"], {"date": x["date"], "spend_ngn": 0, "impressions": 0,
                                         "clicks": 0, "conversations": 0})
        for k in ("spend_ngn", "impressions", "clicks", "conversations"):
            d[k] += x.get(k) or 0
    ends = None
    try:
        ends = engine._aware((await _adapter(db).fetch_adset_schedule(campaign_id)).get("end_time"))
    except Exception:
        pass
    return {
        "campaign_id": campaign_id, "name": r.get("display_name") or "Campaign",
        "totals": t, "daily": [daily[k] for k in sorted(daily)],
        "health": {"last_updated": h.get("last_success_at"), "last_error": h.get("last_error") or "",
                   "landing": h.get("landing") or {}, "source": metrics.SOURCE,
                   "rows_last_pull": h.get("rows_last_pull"), "sample_row": h.get("sample_row") or {},
                   "attribution": metrics.ATTRIBUTION},
        "goal": await engine.load_goal(db, campaign_id),
        "diagnoses": diagnoses, "recommendations": recs,
        "leads": await leads.stats(db, brand, ad_ids, t["spend_ngn"]),
        "follow_ups": await leads.follow_up_gaps(db, brand, ad_ids),
        "creative_test": creative.compare(variants, ad_rows) if variants else None,
        "forecast": forecast.project(rows, ends),
    }


@router.post("/campaigns/{campaign_id}/refresh")
async def refresh(campaign_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    r = await _campaign(db, _brand(ctx), campaign_id)
    return await engine.run_campaign(db, _adapter(db), r,
                                     notify=lambda rec, d, a=None: engine.notify_owner(db, rec, d, a))


@router.put("/campaigns/{campaign_id}/goal")
async def set_goal(campaign_id: str, payload: dict = Body(...),
                   db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Targets the campaign is judged against (§4.1). Without one, benchmarks are the
    campaign's own baseline and marked provisional."""
    r = await _campaign(db, _brand(ctx), campaign_id)

    def num(k):
        try:
            v = float(payload.get(k))
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    goal = {"campaign_id": campaign_id, "brand_id": r.get("brand_id"),
            "target_cpl_ngn": num("target_cpl_ngn"),
            "target_cost_per_qualified_ngn": num("target_cost_per_qualified_ngn"),
            "target_results": num("target_results"),
            "conversion_definition": str(payload.get("conversion_definition") or
                                         "A conversation started from the ad")[:200],
            "updated_by": ctx.get("user_id", ""), "updated_at": datetime.now(timezone.utc)}
    await db[engine.GOALS].update_one({"campaign_id": campaign_id}, {"$set": goal}, upsert=True)
    await engine.event(db, r.get("brand_id"), campaign_id, "goal_updated", actor=ctx.get("user_id", ""))
    return {"goal": goal}


@router.post("/recommendations/{rec_id}/approve")
async def approve(rec_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    brand = _brand(ctx)
    rec = await db[engine.RECOMMENDATIONS].find_one({"id": rec_id, "brand_id": brand})
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    if rec["status"] not in ("proposed", "failed"):
        raise HTTPException(status_code=409, detail=f"Already {rec['status']}")
    if not await _can_change(db, ctx):
        raise HTTPException(status_code=403, detail="Only an admin can approve changes to a campaign.")
    record = await _campaign(db, brand, rec["campaign_id"])
    result = await engine.approve(db, _adapter(db), record, _clean(rec), ctx.get("user_id", ""))
    if result.get("status") == "refused":
        raise HTTPException(status_code=409, detail=result["reason"])
    return {"action": result}


@router.post("/recommendations/{rec_id}/decide")
async def decide(rec_id: str, payload: dict = Body(...),
                 db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                 ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Reject a suggestion, or mark one you carried out yourself as done."""
    brand = _brand(ctx)
    rec = await db[engine.RECOMMENDATIONS].find_one({"id": rec_id, "brand_id": brand})
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    decision = payload.get("decision")
    if decision not in ("rejected", "done"):
        raise HTTPException(status_code=400, detail="decision must be rejected or done")
    now = datetime.now(timezone.utc)
    update = {"status": decision, "decided_by": ctx.get("user_id", ""), "decided_at": now,
              "note": str(payload.get("note") or "")[:300]}
    if decision == "done":
        # A change made outside URI is still measured: log it so its window is evaluated.
        entry = {"action_id": rec_id, "idempotency_key": rec_id, "brand_id": brand,
                 "campaign_id": rec["campaign_id"], "recommendation_id": rec_id,
                 "action_type": rec["action_type"], "parameters": rec.get("parameters") or {},
                 "actor": ctx.get("user_id", ""), "mode": "manual", "status": "executed",
                 "requested_change": rec.get("summary"), "verified": False,
                 "executed_at": now,
                 "evaluate_after": now + timedelta(days=int(rec.get("eval_window_days") or 3))}
        await db[act.ACTIONS].update_one({"idempotency_key": rec_id}, {"$set": entry}, upsert=True)
    await db[engine.RECOMMENDATIONS].update_one({"id": rec_id}, {"$set": update})
    await engine.event(db, brand, rec["campaign_id"], f"recommendation_{decision}",
                       recommendation_id=rec_id, actor=ctx.get("user_id", ""))
    return {"status": decision}


@router.post("/actions/{action_id}/undo")
async def undo(action_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
               ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Reverse an executed change, as a new audited action."""
    brand = _brand(ctx)
    a = await db[act.ACTIONS].find_one({"action_id": action_id, "brand_id": brand})
    if not a or a.get("status") not in ("executed", "unverified") or a.get("mode") == "manual":
        raise HTTPException(status_code=404, detail="Nothing to undo")
    if not await _can_change(db, ctx):
        raise HTTPException(status_code=403, detail="Only an admin can change a campaign.")
    record = await _campaign(db, brand, a["campaign_id"])
    before = a.get("before_state") or {}
    if a["action_type"] == "pause_ad":
        raise HTTPException(status_code=400, detail="Turn the ad back on from the campaign page.")
    reverse = {"pause_campaign": ("resume_campaign", {}),
               "resume_campaign": ("pause_campaign", {}),
               "set_daily_budget": ("set_daily_budget", {
                   "from_ngn": (a.get("parameters") or {}).get("to_ngn"),
                   "to_ngn": before.get("daily_budget_ngn")})}.get(a["action_type"])
    if not reverse or (reverse[0] == "set_daily_budget" and not reverse[1]["to_ngn"]):
        raise HTTPException(status_code=400, detail="This change can't be undone automatically.")
    rec = {"id": f"undo:{action_id}", "action_type": reverse[0], "parameters": reverse[1],
           "summary": f"Undo: {a.get('requested_change')}", "brand_id": brand,
           "campaign_id": a["campaign_id"], "eval_window_days": 1, "executable": True}
    entry = await act.execute(db, _adapter(db), record, rec, actor=ctx.get("user_id", ""), mode="undo")
    await engine.event(db, brand, a["campaign_id"], "action_undone", action_id=action_id,
                       status=entry["status"], actor=ctx.get("user_id", ""))
    return {"action": entry}


@router.get("/history")
async def history(campaign_id: Optional[str] = None,
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Optimization History (§8): every change with trigger, before/after, actor,
    Meta's answer and the measured outcome; plus the event trail."""
    q = {"brand_id": _brand(ctx)}
    if campaign_id:
        q["campaign_id"] = campaign_id
    actions = [_clean(a) for a in await db[act.ACTIONS].find(q).sort("executed_at", -1).limit(100).to_list(100)]
    events = [_clean(e) for e in await db[engine.EVENTS].find(q).sort("at", -1).limit(200).to_list(200)]
    return {"actions": actions, "events": events,
            "track_record": await evaluate.track_record(db, _brand(ctx))}


@router.get("/policy")
async def get_policy(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                     ctx: dict = Depends(get_active_brand_context)) -> dict:
    return {"policy": await engine.load_policy(db, _brand(ctx)),
            "can_change": await _can_change(db, ctx),
            "autonomous_actions": list(pol.AUTONOMOUS_ACTIONS)}


@router.put("/policy")
async def put_policy(payload: dict = Body(...), db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                     ctx: dict = Depends(get_active_brand_context)) -> dict:
    brand = _brand(ctx)
    if not await _can_change(db, ctx):
        raise HTTPException(status_code=403, detail="Only an admin can change automation settings.")
    current = await engine.load_policy(db, brand)
    raw = {**current, **payload}
    # Turning autonomy on is recorded consent: who, when. Anything else keeps it.
    if payload.get("mode") == "autonomous" and current["mode"] != "autonomous":
        if not payload.get("confirm_autonomous"):
            raise HTTPException(status_code=400, detail="Confirm you authorise URI to make these changes on its own.")
        raw["autonomous_authorized_by"] = ctx.get("user_id", "")
        raw["autonomous_authorized_at"] = datetime.now(timezone.utc)
    if payload.get("mode") in ("advisory", "approval"):
        raw["autonomous_authorized_by"], raw["autonomous_authorized_at"] = "", None
    clean = pol.normalize(raw)
    await db[engine.POLICIES].update_one({"brand_id": brand}, {"$set": {**clean, "brand_id": brand,
                                         "updated_by": ctx.get("user_id", ""),
                                         "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    await engine.event(db, brand, "", "policy_updated", actor=ctx.get("user_id", ""), mode=clean["mode"],
                       emergency_stop=clean["emergency_stop"])
    return {"policy": clean}


@router.post("/emergency-stop")
async def emergency_stop(payload: dict = Body(default={}),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    brand = _brand(ctx)
    on = bool(payload.get("on", True))
    await db[engine.POLICIES].update_one({"brand_id": brand}, {"$set": {
        "brand_id": brand, "emergency_stop": on, "updated_by": ctx.get("user_id", ""),
        "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    await engine.event(db, brand, "", "emergency_stop", on=on, actor=ctx.get("user_id", ""))
    return {"policy": await engine.load_policy(db, brand)}


@router.post("/campaigns/{campaign_id}/variants")
async def add_variant(campaign_id: str, payload: dict = Body(...),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                      ctx: dict = Depends(get_active_brand_context)) -> dict:
    r = await _campaign(db, _brand(ctx), campaign_id)
    if not payload.get("ad_id"):
        raise HTTPException(status_code=400, detail="ad_id is required")
    v = await creative.register(db, r.get("brand_id"), campaign_id, str(payload["ad_id"]),
                                payload.get("attributes") or {}, str(payload.get("test_group") or ""),
                                ctx.get("user_id", ""))
    return {"variant": {k: v[k] for k in v if k != "_id"}}


@router.patch("/leads/{conversation_id}")
async def set_lead_stage(conversation_id: str, payload: dict = Body(...),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    """A person records what happened to a lead (§4.6). The provenance is kept."""
    from bson import ObjectId
    from app.agents.inbox.entities import CONVERSATIONS
    stage = payload.get("stage")
    if stage not in leads.STAGES:
        raise HTTPException(status_code=400, detail=f"stage must be one of {', '.join(leads.STAGES)}")
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=404, detail="Not found")
    update = {"lead_stage": stage, "lead_stage_by": ctx.get("user_id", ""),
              "lead_stage_at": datetime.now(timezone.utc), "lead_stage_source": "user"}
    try:
        if payload.get("value") is not None:
            update["lead_value"] = max(0.0, float(payload["value"]))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="value must be a number")
    res = await db[CONVERSATIONS].update_one(
        {"_id": ObjectId(conversation_id), "workspace_id": _brand(ctx)}, {"$set": update})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Not found")
    return {"status": "ok", "stage": stage}


@router.get("/admin/insights-probe/{campaign_id}")
async def insights_probe(campaign_id: str, level: str = "campaign", time_increment: str = "1",
                         date_preset: str = "", since: str = "", until: str = "",
                         fields: str = "date_start,spend,impressions,clicks,actions",
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         token: dict = Depends(_jwt())) -> dict:
    """Ads admins only: one raw, read-only insights query, to see exactly what Meta
    returns for given parameters. Never writes anything."""
    import httpx
    from ..router import _require_ads_admin
    _require_ads_admin(token)
    adapter = _adapter(db)
    params = {"access_token": adapter._access_token, "level": level, "fields": fields, "limit": 100}
    if time_increment:
        params["time_increment"] = time_increment
    if date_preset:
        params["date_preset"] = date_preset
    if since and until:
        import json as _json
        params["time_range"] = _json.dumps({"since": since, "until": until})
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{adapter._graph_base}/{campaign_id}/insights", params=params)
    body = resp.json()
    for row in body.get("data") or []:
        row["actions"] = {a.get("action_type"): a.get("value") for a in row.get("actions") or []
                          if "conversation" in (a.get("action_type") or "")}
    body.pop("paging", None)
    return body


@router.get("/admin/graph-probe")
async def graph_probe(path: str, fields: str = "", db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                      token: dict = Depends(_jwt())) -> dict:
    """Ads admins only: a read-only GET of one Graph API node with URI's ads token, to
    diagnose what Meta holds for a Page, ad set or ad. Never writes."""
    import re as _re
    import httpx
    from ..router import _require_ads_admin
    _require_ads_admin(token)
    if not _re.fullmatch(r"[A-Za-z0-9_/]{1,120}", path) or "token" in path.lower():
        raise HTTPException(status_code=400, detail="path must be a plain node id or edge")
    adapter = _adapter(db)
    params = {"access_token": adapter._access_token}
    if fields:
        params["fields"] = fields
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{adapter._graph_base}/{path.strip('/')}", params=params)
    body = resp.json()
    body.pop("paging", None)
    return body
