"""
Growth demo data: three made-up campaigns on a brand, each shaped to trigger a
different recommendation, so the Growth screen can be shown end to end without a
live campaign.

  · Glow Skincare     two ads, one far better  → pause the weaker ad
  · Weekend Promo     spending, no conversations → pause the campaign
  · Abuja Consults    cheap results, budget-bound → raise the daily budget

They live in their own collection, never in jane_ads_meta_campaigns, so Campaign
Manager, billing and monitoring never see them or call Meta for them. Approving a
change on one updates the demo's own state through DemoAdapter, and the history,
undo and evaluation work as they do for a real campaign.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import metrics

DEMO_CAMPAIGNS = "jane_ads_growth_demo_campaigns"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DemoAdapter:
    """Stands in for Meta on a demo campaign: same calls, the demo's own state."""
    demo = True

    def __init__(self, db) -> None:
        self._db = db

    async def _doc(self, campaign_id: str) -> dict:
        return await self._db[DEMO_CAMPAIGNS].find_one({"campaign_id": campaign_id}) or {}

    async def fetch_adset_schedule(self, campaign_id: str) -> dict:
        d = await self._doc(campaign_id)
        return {"adset_id": d.get("adset_id"), "ad_id": d.get("ad_id"), "status": d.get("status"),
                "effective_status": d.get("status"), "daily_ngn": d.get("daily_ngn"),
                "lifetime_ngn": 0, "start_time": d.get("created_at"),
                "end_time": d.get("ends_at").isoformat() if d.get("ends_at") else None}

    async def set_delivery(self, campaign_id: str, active: bool) -> dict:
        status = "ACTIVE" if active else "PAUSED"
        await self._db[DEMO_CAMPAIGNS].update_one({"campaign_id": campaign_id}, {"$set": {"status": status}})
        return {"status": status, "updated": {"campaign": True, "adset": True, "ad": True}}

    async def read(self, node_id: str, fields: str) -> dict:
        d = await self._db[DEMO_CAMPAIGNS].find_one({"$or": [{"campaign_id": node_id},
                                                             {f"ads.{node_id}": {"$exists": True}}]}) or {}
        if node_id in (d.get("ads") or {}):
            return {"id": node_id, "status": d["ads"][node_id], "effective_status": d["ads"][node_id]}
        return {"id": node_id, "status": d.get("status"), "effective_status": d.get("status")}

    async def post(self, node_id: str, body: dict) -> dict:
        if "daily_budget" in body:
            await self._db[DEMO_CAMPAIGNS].update_one(
                {"adset_id": node_id}, {"$set": {"daily_ngn": body["daily_budget"] / 100}})
        elif "status" in body:
            await self._db[DEMO_CAMPAIGNS].update_one(
                {f"ads.{node_id}": {"$exists": True}}, {"$set": {f"ads.{node_id}": body["status"]}})
        return {"success": True}


async def refresh(db, record: dict) -> None:
    """A demo's data is always 'just fetched'."""
    await db[metrics.HEALTH].update_one({"campaign_id": record["campaign_id"]},
                                        {"$set": {"last_success_at": _now(), "last_error": ""}})


def _day(n: int) -> str:
    return (_now().date() - timedelta(days=n)).isoformat()


def _rows(cid: str, brand: str, ad_id: str, days: dict) -> list[dict]:
    """days: {days_ago: (spend, impressions, clicks, conversations)}"""
    out = []
    for n, (spend, imp, clicks, conv) in days.items():
        out.append({"campaign_id": cid, "brand_id": brand, "level": "ad", "ad_id": ad_id,
                    "ad_name": ad_id, "adset_id": f"{cid}-adset", "date": _day(n),
                    "spend_ngn": float(spend), "impressions": imp, "reach": int(imp * 0.7),
                    "clicks": clicks, "conversations": conv, "currency": "NGN",
                    "source": "demo", "reporting_window": "1d",
                    "attribution_window": metrics.ATTRIBUTION, "retrieved_at": _now()})
    return out


def _campaign_rows(ad_rows: list[dict]) -> list[dict]:
    by_day: dict[str, dict] = {}
    for r in ad_rows:
        d = by_day.setdefault(r["date"], {**r, "level": "campaign", "ad_id": "", "ad_name": "",
                                          "spend_ngn": 0.0, "impressions": 0, "reach": 0,
                                          "clicks": 0, "conversations": 0})
        for k in ("spend_ngn", "impressions", "reach", "clicks", "conversations"):
            d[k] += r[k]
    return list(by_day.values())


def _plans(brand: str) -> list[dict]:
    tag = brand[-6:]
    glow, promo, abuja = f"demo-glow-{tag}", f"demo-promo-{tag}", f"demo-abuja-{tag}"
    days = range(1, 13)
    return [
        {"campaign_id": glow, "display_name": "Glow Skincare — WhatsApp (demo)", "daily_ngn": 3000,
         "budget_ngn": 45000,
         "ads": {f"{glow}-before-after": "ACTIVE", f"{glow}-flatlay": "ACTIVE"},
         "rows": _rows(glow, brand, f"{glow}-before-after", {n: (1500, 3000, 60, 9) for n in days})
         + _rows(glow, brand, f"{glow}-flatlay", {n: (1500, 3000, 55, 2) for n in days}),
         "variants": {f"{glow}-before-after": {"hook": "Before and after", "format": "Photo"},
                      f"{glow}-flatlay": {"hook": "Product flatlay", "format": "Photo"}}},
        {"campaign_id": promo, "display_name": "Weekend Promo — Lagos (demo)", "daily_ngn": 2000,
         "budget_ngn": 30000,
         "ads": {f"{promo}-ad": "ACTIVE"},
         "ad_review": [{"ad_id": f"{promo}-ad", "name": "Weekend promo ad", "status": "ACTIVE",
                        "headline": False, "description": False,
                        "problems": ["No headline", "No description",
                                     "Ad undeliverable to Instagram Feed — edit your video to meet the requirements"]}],
         "rows": _rows(promo, brand, f"{promo}-ad",
                       {n: ((2000, 4000, 50, 4) if n > 3 else (2000, 3500, 45, 0)) for n in days})},
        {"campaign_id": abuja, "display_name": "Abuja Consultations — Leads (demo)", "daily_ngn": 2500,
         "budget_ngn": 40000, "target_cpl_ngn": 600,
         "ads": {f"{abuja}-ad": "ACTIVE"},
         "rows": _rows(abuja, brand, f"{abuja}-ad", {n: (2500, 5000, 90, 7) for n in days}),
         "past_action": True},
    ]


async def clear(db, brand_id: str) -> int:
    from . import actions as act
    from .creative import VARIANTS
    from .engine import DIAGNOSES, EVENTS, GOALS, RECOMMENDATIONS
    ids = [d["campaign_id"] async for d in db[DEMO_CAMPAIGNS].find({"brand_id": brand_id}, {"campaign_id": 1})]
    if not ids:
        return 0
    q = {"campaign_id": {"$in": ids}}
    for coll in (DEMO_CAMPAIGNS, metrics.SNAPSHOTS, metrics.HEALTH, DIAGNOSES, RECOMMENDATIONS,
                 act.ACTIONS, EVENTS, GOALS, VARIANTS):
        await db[coll].delete_many(q)
    return len(ids)


async def seed(db, brand_id: str, user_id: str) -> list[dict]:
    """Replace this brand's demo campaigns with fresh ones and run Growth over them."""
    from . import actions as act
    from . import engine
    from .creative import VARIANTS

    await clear(db, brand_id)
    out = []
    for p in _plans(brand_id):
        cid = p["campaign_id"]
        ad_ids = list(p["ads"])
        record = {"campaign_id": cid, "brand_id": brand_id, "user_id": user_id, "demo": True,
                  "display_name": p["display_name"], "adset_id": f"{cid}-adset", "ad_id": ad_ids[0],
                  "ads": p["ads"], "ad_review": p.get("ad_review") or [], "status": "ACTIVE", "daily_ngn": p["daily_ngn"],
                  "launch_daily_ngn": p["daily_ngn"], "budget_ngn": p["budget_ngn"],
                  "ad_spend_markup": 1.111111, "created_at": _now() - timedelta(days=12),
                  "ends_at": _now() + timedelta(days=6)}
        await db[DEMO_CAMPAIGNS].insert_one(dict(record))
        camp_rows = _campaign_rows(p["rows"])
        await db[metrics.SNAPSHOTS].insert_many(p["rows"] + camp_rows)
        life = {k: sum(r[k] for r in camp_rows) for k in ("spend_ngn", "impressions", "reach", "clicks",
                                                          "conversations")}
        await db[metrics.HEALTH].insert_one({
            "campaign_id": cid, "brand_id": brand_id, "last_success_at": _now(), "last_error": "",
            "lifetime": life, "daily_spend_ngn": life["spend_ngn"], "daily_complete": True,
            "landing": {"checked": False}})
        if p.get("target_cpl_ngn"):
            await db[engine.GOALS].insert_one({"campaign_id": cid, "brand_id": brand_id,
                                               "target_cpl_ngn": p["target_cpl_ngn"],
                                               "conversion_definition": "A conversation started from the ad"})
        for i, (ad_id, attrs) in enumerate((p.get("variants") or {}).items(), start=1):
            await db[VARIANTS].insert_one({"brand_id": brand_id, "campaign_id": cid, "ad_id": ad_id,
                                           "test_group": "hooks", "version": i, "attributes": attrs,
                                           "created_at": _now()})
        if p.get("past_action"):
            # A change made last week, already measured — what History looks like.
            at = _now() - timedelta(days=9)
            await db[act.ACTIONS].insert_one({
                "action_id": f"{cid}-past", "idempotency_key": f"{cid}-past", "brand_id": brand_id,
                "campaign_id": cid, "recommendation_id": f"{cid}-past", "action_type": "refresh_creative",
                "parameters": {}, "actor": user_id, "mode": "manual", "status": "executed",
                "requested_change": "Swapped the image for a photo of a real consultation.",
                "verified": False, "executed_at": at, "evaluate_after": at + timedelta(days=5),
                "evaluation": {"verdict": "improved", "metric": "cost_per_conversation_ngn",
                               "before": 512.0, "after": 357.0, "change": -0.303,
                               "note": "Associated with the change, not proof it caused it.",
                               "evaluated_at": at + timedelta(days=5), "window_days": 5}})
        result = await engine.run_campaign(db, DemoAdapter(db), record)
        out.append({"campaign_id": cid, "name": p["display_name"], **result})
    return out
