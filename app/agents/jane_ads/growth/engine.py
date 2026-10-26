"""
One growth pass per campaign (PRD §6 event flow):

  metric_snapshot_received → data_validated → anomaly_detected → diagnosis_created
  → recommendation_created → approval_requested | action_executed → action_verified
  → outcome_evaluated

Run hourly by the scheduler; also on demand from the Growth screen.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import actions as act
from . import detect as det
from . import diagnose as dia
from . import evaluate, leads, metrics
from . import policy as pol

DIAGNOSES = "jane_ads_growth_diagnoses"
RECOMMENDATIONS = "jane_ads_growth_recommendations"
POLICIES = "jane_ads_growth_policy"
GOALS = "jane_ads_growth_goals"
EVENTS = "jane_ads_growth_events"
CAMPAIGNS = "jane_ads_meta_campaigns"

LANDING_RECHECK = timedelta(hours=12)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(at) -> Optional[datetime]:
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(at, datetime):
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


async def event(db, brand_id: str, campaign_id: str, kind: str, **data) -> None:
    """The audit trail: every step that matters, in order, with who did it."""
    await db[EVENTS].insert_one({"brand_id": brand_id, "campaign_id": campaign_id, "type": kind,
                                 "at": _now(), **data})


async def load_policy(db, brand_id: str) -> dict:
    return pol.normalize(await db[POLICIES].find_one({"brand_id": brand_id}))


async def load_goal(db, campaign_id: str) -> dict:
    return await db[GOALS].find_one({"campaign_id": campaign_id}, {"_id": 0}) or {}


async def market_signals(db, brand_id: str) -> list[dict]:
    """Recent market observations for this brand, labelled as hypotheses to test."""
    since = _now() - timedelta(days=14)
    out = []
    try:
        async for m in db["mi_insights"].find(
                {"brand_id": brand_id, "status": "active",
                 "$or": [{"created_at": {"$gte": since}}, {"updated_at": {"$gte": since}}]}).limit(3):
            out.append({"headline": m.get("headline", ""), "suggestion": m.get("suggested_next_step", ""),
                        "source": "URI Market Intelligence",
                        "observed_at": (m.get("updated_at") or m.get("created_at")),
                        "label": "A market signal to test, not a measured cause."})
    except Exception:
        return []
    return out


def adapter_for(db):
    from app.core.config import settings
    from ..adapters.meta import MetaAdPlatformAdapter
    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        return None
    return MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)


async def find_campaign(db, campaign_id: str) -> Optional[dict]:
    from .demo import DEMO_CAMPAIGNS
    return await db[CAMPAIGNS].find_one({"campaign_id": campaign_id}) or \
        await db[DEMO_CAMPAIGNS].find_one({"campaign_id": campaign_id})


def adapter_for_record(db, record: dict):
    if record.get("demo"):
        from .demo import DemoAdapter
        return DemoAdapter(db)
    return adapter_for(db)


async def context(db, adapter, record: dict, policy: dict, pull: bool = True) -> dict:
    """Everything detection and the policy check need, gathered once."""
    cid, brand = record["campaign_id"], record.get("brand_id")
    if record.get("demo"):
        from .demo import refresh
        await refresh(db, record)
        pull = False
    if pull:
        await event(db, brand, cid, "metric_snapshot_requested")
        res = await metrics.pull(db, adapter, record)
        await event(db, brand, cid, "metric_snapshot_received" if res["ok"] else "metric_snapshot_failed",
                    rows=res["rows"], error=res["error"])
    health = await metrics.health(db, cid)
    rows = await metrics.load(db, cid)
    ad_rows = await metrics.load(db, cid, level="ad")
    try:
        sched = await adapter.fetch_adset_schedule(cid)
    except Exception as e:
        sched = {"error": str(e)[:200]}
    goal = await load_goal(db, cid)
    ad_ids = sorted({r["ad_id"] for r in ad_rows if r.get("ad_id")} | {record.get("ad_id") or ""} - {""})
    spend = (health.get("lifetime") or {}).get("spend_ngn") or \
        sum(float(r.get("spend_ngn") or 0) for r in rows)
    lead_stats = await leads.stats(db, brand, ad_ids, spend)
    followups = await leads.follow_up_gaps(db, brand, ad_ids)

    landing = health.get("landing") or {}
    if pull and (not landing or _aware(landing.get("checked_at")) is None or
                 _now() - _aware(landing.get("checked_at")) > LANDING_RECHECK):
        url = await metrics.destination_url(adapter, record.get("ad_id") or "")
        landing = await metrics.check_landing_page(url) if url else {"checked": False}
        landing.setdefault("checked_at", _now().isoformat())
        await db[metrics.HEALTH].update_one({"campaign_id": cid}, {"$set": {"landing": landing}})

    ad_review: list[dict] = []
    if not record.get("demo") and pull:
        ad_review = await metrics.review_ads(adapter, cid)
        await db[metrics.HEALTH].update_one({"campaign_id": cid}, {"$set": {"ad_review": ad_review}})
    elif record.get("demo"):
        ad_review = record.get("ad_review") or []
    else:
        ad_review = health.get("ad_review") or []
    fresh = _aware(health.get("last_success_at"))
    return {
        "record": record, "rows": rows, "ad_rows": ad_rows, "health": health, "schedule": sched, "goal": goal,
        "ad_ids": ad_ids, "leads": lead_stats, "followups": followups, "landing": landing,
        "detect": {
            "today": _now().date(), "status": sched.get("effective_status") or "",
            "retrieved_at": fresh,
            "fetch_error": health.get("last_error") or sched.get("error") or "",
            "daily_budget_ngn": sched.get("daily_ngn") or 0,
            "target_cpl_ngn": goal.get("target_cpl_ngn"),
            "leads": lead_stats, "landing": landing, "followups": followups,
            "ad_rows": ad_rows,
            "ad_review": ad_review,
            "daily_complete": health.get("daily_complete"),
            "lifetime_spend_ngn": (health.get("lifetime") or {}).get("spend_ngn"),
            "daily_spend_ngn": health.get("daily_spend_ngn"),
        },
        "state": {
            "data_fresh": bool(fresh and _now() - fresh <= det.STALE_AFTER),
            "active": (sched.get("effective_status") or "").upper() == "ACTIVE",
            "launch_daily_ngn": record.get("launch_daily_ngn") or sched.get("daily_ngn"),
            "last_action_at": await act.last_action_at(db, cid),
            "budget_raised_7d_pct": await act.budget_raised_pct_7d(db, cid),
            # Paid in full at launch: billing stops it at the paid budget, so a
            # faster daily rate can't spend more than was paid.
            "wallet_ngn": float("inf") if record.get("budget_ngn") else 0,
            "markup": float(record.get("ad_spend_markup") or 1),
        },
        "policy": policy,
    }


async def _open_blocker(db, cid: str) -> str:
    d = await db[DIAGNOSES].find_one({"campaign_id": cid, "status": "open", "blocks_actions": True})
    return d["title"] if d else ""


async def run_campaign(db, adapter, record: dict, notify=None) -> dict:
    # Demo campaigns (scripts/seed_growth_demo.py) have no Meta counterpart: a pull
    # would fail and resolve every seeded diagnosis as "No longer detected".
    if record.get("_seeded"):
        return {"findings": 0, "new_recommendations": 0, "executed": 0}
    cid, brand = record["campaign_id"], record.get("brand_id")
    policy = await load_policy(db, brand)
    ctx = await context(db, adapter, record, policy)
    findings = det.detect(ctx["rows"], ctx["detect"])
    await event(db, brand, cid, "data_validated", fresh=ctx["state"]["data_fresh"])
    history = await evaluate.track_record(db, brand)
    market = await market_signals(db, brand)

    seen, created = set(), []
    for f in findings:
        fp = det.fingerprint(cid, f)
        seen.add(fp)
        d = dia.diagnose(f, {"policy": policy, "history": history, "market": market})
        existing = await db[DIAGNOSES].find_one({"fingerprint": fp, "status": "open"})
        if existing:
            await db[DIAGNOSES].update_one({"_id": existing["_id"]}, {"$set": {
                "evidence": d["evidence"], "hypotheses": d["hypotheses"], "confidence": d["confidence"],
                "window": d["window"], "updated_at": _now()}})
            continue
        did = uuid.uuid4().hex
        await db[DIAGNOSES].insert_one({**d, "id": did, "fingerprint": fp, "brand_id": brand,
                                        "campaign_id": cid, "campaign_name": record.get("display_name") or "",
                                        "status": "open", "created_at": _now(), "updated_at": _now()})
        await event(db, brand, cid, "anomaly_detected", issue=f["issue_type"], diagnosis_id=did)
        await event(db, brand, cid, "diagnosis_created", diagnosis_id=did, confidence=d["confidence"])
        for r in d["recommendations"]:
            rid = uuid.uuid4().hex
            doc = {**r, "id": rid, "diagnosis_id": did, "brand_id": brand, "campaign_id": cid,
                   "status": "proposed", "created_at": _now(),
                   "approval_required": not (policy["mode"] == "autonomous"
                                             and r["action_type"] in policy["allowed_actions"])}
            await db[RECOMMENDATIONS].insert_one(doc)
            await event(db, brand, cid, "recommendation_created", recommendation_id=rid,
                        action=r["action_type"])
            created.append(doc)
        if notify and d["severity"] in ("high", "medium") and policy["notify"]:
            await notify(record, d)

    # Findings that stopped firing are resolved, with the reason on record.
    async for d in db[DIAGNOSES].find({"campaign_id": cid, "status": "open"}):
        if d["fingerprint"] not in seen:
            await db[DIAGNOSES].update_one({"_id": d["_id"]}, {"$set": {
                "status": "resolved", "resolved_at": _now(), "resolution": "No longer detected"}})
            await db[RECOMMENDATIONS].update_many({"diagnosis_id": d["id"], "status": "proposed"},
                                                  {"$set": {"status": "superseded"}})

    executed = []
    if policy["mode"] == "autonomous" and not policy["emergency_stop"]:
        ctx["state"]["blocked_by"] = await _open_blocker(db, cid)
        for rec in created:
            ok, why = pol.check(policy, rec, ctx["state"], by_user=False)
            if not ok:
                continue
            entry = await act.execute(db, adapter, record, rec, actor="uri", mode="autonomous")
            await _after_execution(db, rec, entry)
            executed.append(entry)
            ctx["state"]["last_action_at"] = entry.get("executed_at")
            if notify:
                await notify(record, None, entry)
    return {"findings": len(findings), "new_recommendations": len(created), "executed": len(executed)}


async def _after_execution(db, rec: dict, entry: dict) -> None:
    status = {"executed": "executed", "unverified": "unverified"}.get(entry["status"], "failed")
    await db[RECOMMENDATIONS].update_one({"id": rec["id"]}, {"$set": {
        "status": status, "action_id": entry.get("action_id"), "executed_at": entry.get("executed_at")}})
    await event(db, rec["brand_id"], rec["campaign_id"], "action_executed", recommendation_id=rec["id"],
                action=rec["action_type"], actor=entry.get("actor"), status=entry["status"])
    if entry["status"] in ("executed", "unverified"):
        await event(db, rec["brand_id"], rec["campaign_id"], "action_verified",
                    recommendation_id=rec["id"], verified=entry.get("verified", False))


async def manual_change(db, adapter, record: dict, action: str, params: dict, actor: str) -> dict:
    """A change the owner picks themselves on the Growth screen. Same executor as an
    approved recommendation — checked, applied on Meta, read back, logged, undoable."""
    labels = {"pause_ad": "Paused an ad", "resume_ad": "Turned an ad back on",
              "pause_campaign": "Paused the campaign", "resume_campaign": "Resumed the campaign",
              "set_daily_budget": f"Set the daily budget to ₦{float(params.get('to_ngn') or 0):,.0f}"}
    policy = await load_policy(db, record.get("brand_id"))
    ctx = await context(db, adapter, record, policy, pull=False)
    if action == "set_daily_budget":
        params = {**params, "from_ngn": (ctx["schedule"] or {}).get("daily_ngn") or 0}
    rec = {"id": f"manual:{uuid.uuid4().hex}", "action_type": action, "parameters": params,
           "summary": labels.get(action, action), "executable": True, "eval_window_days": 3,
           "brand_id": record.get("brand_id"), "campaign_id": record["campaign_id"]}
    ok, why = pol.check(policy, rec, ctx["state"], by_user=True, manual=True)
    if not ok:
        return {"status": "refused", "reason": why}
    entry = await act.execute(db, adapter, record, rec, actor=actor, mode="manual_change")
    await event(db, rec["brand_id"], rec["campaign_id"], "manual_change", action=action,
                actor=actor, status=entry["status"])
    return entry


async def approve(db, adapter, record: dict, rec: dict, actor: str) -> dict:
    """A person approved a recommendation: re-check everything, then run it."""
    policy = await load_policy(db, record.get("brand_id"))
    ctx = await context(db, adapter, record, policy, pull=False)
    ctx["state"]["blocked_by"] = await _open_blocker(db, record["campaign_id"])
    ok, why = pol.check(policy, rec, ctx["state"], by_user=True)
    if not ok:
        return {"status": "refused", "reason": why}
    await db[RECOMMENDATIONS].update_one({"id": rec["id"]}, {"$set": {
        "status": "approved", "approved_by": actor, "approved_at": _now()}})
    await event(db, rec["brand_id"], rec["campaign_id"], "approval_granted", recommendation_id=rec["id"],
                actor=actor)
    entry = await act.execute(db, adapter, record, rec, actor=actor, mode=policy["mode"])
    await _after_execution(db, rec, entry)
    return entry


async def run_all(db, notify=None) -> dict:
    adapter = adapter_for(db)
    if adapter is None:
        return {"campaigns": 0, "skipped": "Meta ads not configured"}
    totals = {"campaigns": 0, "findings": 0, "new_recommendations": 0, "executed": 0, "errors": 0}
    # Finished campaigns keep being read for a month — late attribution lands after
    # the end, and their history is what the Growth screen and evaluations show —
    # then stop costing API calls.
    recent = _now() - timedelta(days=30)
    async for record in db[CAMPAIGNS].find({"brand_id": {"$exists": True, "$ne": None},
                                            "campaign_id": {"$exists": True},
                                            "$or": [{"ended_notified": {"$ne": True}},
                                                    {"created_at": {"$gte": recent}}]}):
        try:
            r = await run_campaign(db, adapter, record, notify)
            totals["campaigns"] += 1
            for k in ("findings", "new_recommendations", "executed"):
                totals[k] += r[k]
        except Exception as e:  # one campaign never stops the sweep
            totals["errors"] += 1
            print(f"[growth] {record.get('campaign_id')}: {e}", flush=True)

    from .demo import DEMO_CAMPAIGNS, DemoAdapter
    async for record in db[DEMO_CAMPAIGNS].find({}):
        try:
            await run_campaign(db, DemoAdapter(db), record, None)
        except Exception as e:
            print(f"[growth] demo {record.get('campaign_id')}: {e}", flush=True)

    async def rows_for(cid):
        return await metrics.load(db, cid, days=60)
    totals["evaluated"] = await evaluate.evaluate_due(db, rows_for)
    return totals


async def notify_owner(db, record: dict, diagnosis: Optional[dict], action: Optional[dict] = None) -> None:
    """Tell the campaign's owner: a new problem worth their attention, or a change URI
    made on its own (every automated change is announced)."""
    from app.core.config import settings
    from app.services import brand_notifier

    name = record.get("display_name") or "Your campaign"
    base = (settings.WEB_APP_URL or "https://www.urisocial.com").strip("'\"").rstrip("/")
    link = f"{base}/workspace/?tab=campaigns"
    if action:
        title = f"URI changed {name}"
        message = (f"{action.get('requested_change') or action['action_type']} "
                   f"({'verified' if action.get('verified') else action['status']}). "
                   "You can undo it from Growth → History.")
        key = f"action:{action['action_id']}"
    else:
        top = (diagnosis.get("recommendations") or [{}])[0]
        title = f"{name}: {diagnosis['title']}"
        message = (f"{diagnosis['hypotheses'][0]['why'] if diagnosis.get('hypotheses') else ''} "
                   f"Suggested: {top.get('summary', 'take a look')}").strip()
        key = f"diag:{record['campaign_id']}:{diagnosis['issue_type']}"
    try:
        await brand_notifier.notify(db, record.get("user_id") or "", record.get("brand_id"),
                                    "ad_growth_alert", title=title, message=message, link=link,
                                    metadata={"campaign_id": record["campaign_id"]},
                                    dedupe_key=key, dedupe_hours=24)
    except Exception as e:
        print(f"[growth] notify failed for {record.get('campaign_id')}: {e}", flush=True)
