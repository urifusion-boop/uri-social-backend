"""
Creative testing (PRD §4.7). Variants are registered with what makes them
different (hook, value proposition, format, call to action); results come from
the same daily snapshots as everything else. A winner is only declared when
both sides delivered enough and the gap holds up statistically.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .stats import totals, two_proportion_p

VARIANTS = "jane_ads_creative_variants"
MIN_IMPRESSIONS = 1000
MIN_CONVERSATIONS = 5


async def register(db, brand_id: str, campaign_id: str, ad_id: str, attributes: dict,
                   test_group: str, actor: str) -> dict:
    existing = await db[VARIANTS].count_documents({"campaign_id": campaign_id, "test_group": test_group})
    doc = {"brand_id": brand_id, "campaign_id": campaign_id, "ad_id": ad_id,
           "test_group": test_group or "default", "version": existing + 1,
           "attributes": {k: str(v)[:200] for k, v in (attributes or {}).items()
                          if k in ("hook", "value_proposition", "format", "cta", "image", "notes")},
           "created_by": actor, "created_at": datetime.now(timezone.utc)}
    await db[VARIANTS].update_one({"campaign_id": campaign_id, "ad_id": ad_id},
                                  {"$set": doc}, upsert=True)
    return doc


def compare(variants: list[dict], rows: list[dict]) -> dict:
    """Per-variant results and, only if earned, a winner."""
    by_ad = {v["ad_id"]: v for v in variants}
    results = []
    for ad_id, v in by_ad.items():
        t = totals([r for r in rows if r.get("ad_id") == ad_id])
        enough = t["impressions"] >= MIN_IMPRESSIONS and t["conversations"] >= MIN_CONVERSATIONS
        results.append({"ad_id": ad_id, "version": v.get("version"), "attributes": v.get("attributes"),
                        "spend_ngn": round(t["spend_ngn"], 2), "impressions": int(t["impressions"]),
                        "clicks": int(t["clicks"]), "conversations": int(t["conversations"]),
                        "cost_per_conversation_ngn": round(t["cost_per_conversation_ngn"], 2)
                        if t["cost_per_conversation_ngn"] else None,
                        "enough_data": enough})
    ready = [r for r in results if r["enough_data"]]
    verdict = {"winner": None, "p_value": None,
               "reason": "Not enough delivery yet — each variant needs at least "
                         f"{MIN_IMPRESSIONS:,} impressions and {MIN_CONVERSATIONS} conversations."}
    if len(ready) >= 2:
        ready.sort(key=lambda r: r["conversations"] / r["impressions"], reverse=True)
        a, b = ready[0], ready[1]
        p = two_proportion_p(a["conversations"], a["impressions"], b["conversations"], b["impressions"])
        if p is not None and p < 0.05:
            verdict = {"winner": a["ad_id"], "p_value": round(p, 4),
                       "reason": "The leading variant's conversation rate is higher than the next, "
                                 "beyond what chance would usually explain."}
        else:
            verdict = {"winner": None, "p_value": round(p, 4) if p is not None else None,
                       "reason": "No clear winner yet — the difference could still be chance."}
    return {"variants": results, **verdict}
