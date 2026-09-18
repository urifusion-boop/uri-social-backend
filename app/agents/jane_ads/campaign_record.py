"""
Jane + Ads — the campaign decision record (CI-SPEC-01 Part 1).

Results are recoverable; decisions are not. Meta holds spend, reach and conversation
counts for any campaign that ever ran, and they can be pulled back at any time. What
is destroyed at the end of every session is the other half: which platform Jane chose
and why, which plans she ranked that the client rejected, what the client changed on
the plan card, which corpus records shaped it and at what version.

Meta cannot see any of that, and no API can reconstruct it later. Every campaign that
runs without a record is that half permanently lost.

Two fields carry most of the value and neither exists anywhere else:

**Rejected plans.** `plans_generated` keeps every variant Jane produced, not just the
one that ran. A plan she ranks first that clients keep declining means the ranking is
wrong, and storing only the winner throws away the comparison that would show it.

**Recommended vs selected.** When they differ, that is the most direct measure of
whether Jane's strategic reasoning matches what clients actually want.

Written once at launch and never rewritten, except for `results`, which is backfilled
from Meta on completion (CI-SPEC-01 §1.6 — that half is recoverable, so it is
deliberately the only mutable part).

Every write is best-effort: a failure here must never fail a launch the client has
already paid for. A lost record costs a data point; a failed launch costs a campaign.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

RECORDS = "jane_ads_campaign_records"
VARIANTS = "jane_ads_plan_variant_sets"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def save_generated_variants(
    db, *, variant_group_id: str, brand_id: str, business_id: str,
    variants: list[dict], recommended_rank: Optional[int] = None,
) -> None:
    """Persist the FULL ranked set at the moment Jane generates it.

    This has to happen here, not at launch: the client is shown every variant and
    picks one, and by launch time only the winner is still in hand. The rejected ones
    are the comparison — without them there is no way to learn that a plan Jane ranks
    first is one clients consistently decline.

    Keyed by variant_group_id so the launch can find the set it came from.
    """
    if db is None or not variant_group_id:
        return
    try:
        await db[VARIANTS].update_one(
            {"variant_group_id": variant_group_id},
            {"$set": {
                "variant_group_id": variant_group_id,
                "brand_id": brand_id,
                "business_id": business_id,
                "variants": variants,
                "recommended_rank": recommended_rank,
                "generated_at": _now(),
            }},
            upsert=True,
        )
    except Exception as e:
        print(f"[CampaignRecord] variant set not saved for {variant_group_id}: {e}", flush=True)


async def _load_variant_set(db, variant_group_id: str) -> dict:
    if db is None or not variant_group_id:
        return {}
    try:
        return await db[VARIANTS].find_one({"variant_group_id": variant_group_id}, {"_id": 0}) or {}
    except Exception:
        return {}


def _bucket_context(understood: dict, plan: dict, req: dict, stated_budget_ngn: float) -> dict:
    """The keys every future comparison is sliced by (CI-SPEC-01 §2.2).

    Tagged at write time because buckets cannot be formed retroactively — a campaign
    that ran without its category and city can never be put into one.

    Coarse on purpose. With a few dozen clients, finer keys guarantee no bucket ever
    reaches the sample size that licenses a claim.
    """
    from .decision_engine import budget_tier_for

    geo = plan.get("geo") or {}
    return {
        # Primary — sliced on from day one.
        "business_category": (understood.get("category") or "").strip().lower() or "other",
        "city": (understood.get("city") or geo.get("city") or "").strip().lower() or "other",
        "budget_tier": budget_tier_for(stated_budget_ngn),
        "platform": "meta",
        # Secondary — captured now, sliced on only once a primary bucket fills.
        "area": [p.get("name") for p in (geo.get("pins") or []) if p.get("name")],
        "conversion_location": plan.get("destination_type") or "",
        "purchase_behaviour": understood.get("stated_behaviour") or "",
        "geo_strategy": understood.get("geo_mode") or "",
    }


async def write_campaign_record(
    db, *, campaign_id: str, brand_id: str, business_id: str, plan_doc: dict,
    stated_budget_ngn: float, ad_spend_ngn: float, service_fee_ngn: float,
) -> None:
    """Write the record for a campaign that just launched.

    `plan_doc` is the stored pending plan — it already carries the understanding, the
    built plan, the selected variant and the corpus citations, so nothing extra has to
    be threaded through the launch path.

    Best-effort by design: never raises, and never blocks a launch.
    """
    if db is None or not campaign_id:
        return
    try:
        plan = plan_doc.get("plan") or {}
        req = plan_doc.get("req") or {}
        understood = plan_doc.get("understood") or {}
        creative = plan.get("creative") or {}
        geo = plan.get("geo") or {}

        selected = plan_doc.get("selected_plan_variant") or {}
        variant_set = await _load_variant_set(db, plan_doc.get("variant_group_id", ""))
        generated = variant_set.get("variants") or []
        recommended = next((v for v in generated if v.get("recommended")), None)

        record = {
            "campaign_id": campaign_id,
            "brand_id": brand_id,
            "business_id": business_id,
            "created_at": _now(),
            "closed_at": None,

            "context": _bucket_context(understood, plan, req, stated_budget_ngn),

            "strategy": {
                "sells": understood.get("offer_type") or "",
                "trigger": selected.get("trigger") or "",
                "geo_strategy": understood.get("geo_mode") or "",
                "geo_pockets": [p.get("name") for p in (geo.get("pins") or []) if p.get("name")],
                "platform_chosen": plan_doc.get("jane_platforms") or ["meta"],
                "forced_to_meta": bool(plan_doc.get("forced_to_meta")),
                "purchase_behaviour": understood.get("stated_behaviour") or "",
                # Whether the client corrected the business-type default. A category
                # where overrides dominate is a heuristic that is wrong (§1.2).
                "behaviour_source": (
                    "user_override" if req.get("behaviour") and understood.get("stated_behaviour")
                    and req["behaviour"] != understood["stated_behaviour"]
                    else "business_type_default"
                ),
                # EVERY plan, not just the winner — the whole point of the record.
                "plans_generated": generated,
                "plan_selected": selected,
                "plan_recommended": recommended,
                # First-class signal: divergence means the ranking disagrees with the
                # client, which no other field exposes.
                "recommendation_diverged": bool(
                    recommended and selected
                    and recommended.get("rank") != selected.get("rank")
                ),
                "corpus_coverage": plan.get("corpus_coverage") or creative.get("corpus_coverage") or "none",
                "corpus_records_cited": plan.get("corpus_citations") or [],
            },

            "creative": {
                "format_id": creative.get("vsg01_format_id") or "",
                "asset_source": req.get("creative_source") or "generate",
                "media_type": "video" if creative.get("is_video") else "static",
                "headline": creative.get("headline") or "",
                "image_url": creative.get("image_url") or "",
                "destination_type": plan.get("destination_type") or "",
            },

            "budget": {
                "stated_ngn": stated_budget_ngn,
                "effective_spend_ngn": ad_spend_ngn,
                "service_fee_ngn": service_fee_ngn,
                "duration_days": plan.get("days") or req.get("days") or 0,
                "budget_tier": _bucket_context(understood, plan, req, stated_budget_ngn)["budget_tier"],
                "budget_source": "user_stated" if req.get("budget_ngn") else "derived_from_goal",
                "goal_stated": understood.get("desired_conversions"),
            },

            # Filled by the client's own edits (§1.5) and by Meta on completion (§1.6).
            "modifications": [],
            "results": None,
        }
        await db[RECORDS].update_one(
            {"campaign_id": campaign_id}, {"$setOnInsert": record}, upsert=True,
        )
        print(f"[CampaignRecord] wrote record for {campaign_id} "
              f"({len(generated)} plans generated, diverged="
              f"{record['strategy']['recommendation_diverged']})", flush=True)
    except Exception as e:
        # A lost record costs one data point; a failed launch costs a campaign the
        # client has already been charged for.
        print(f"[CampaignRecord] record not written for {campaign_id}: {e}", flush=True)
