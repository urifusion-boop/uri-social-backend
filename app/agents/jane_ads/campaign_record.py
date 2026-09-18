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

import hashlib

from datetime import datetime, timezone
from typing import Any, Optional

RECORDS = "jane_ads_campaign_records"
VARIANTS = "jane_ads_plan_variant_sets"
REVISIONS = "jane_ads_plan_revisions"


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


# The plan-card lines a client can change (CI-SPEC-01 §1.5). Aggregated across
# campaigns these answer which of Jane's defaults are wrong: a field most clients in a
# category edit is a default that should change, and there is no other way to see it.
_TRACKED_FIELDS = {
    "platform": lambda plan, req: sorted(req.get("platforms") or []),
    "geo": lambda plan, req: sorted(
        p.get("name") for p in ((plan.get("geo") or {}).get("pins") or []) if p.get("name")
    ),
    "audience": lambda plan, req: (plan.get("audience_targeting") or {}).get("flexible_spec"),
    "budget": lambda plan, req: req.get("budget_ngn"),
    "duration": lambda plan, req: plan.get("days") or req.get("days"),
    "creative": lambda plan, req: ((plan.get("creative") or {}).get("image_url") or ""),
    "destination": lambda plan, req: plan.get("destination_type"),
}


def diff_plans(previous: dict, current: dict) -> list[dict]:
    """What the client changed between two builds of the same campaign.

    Called when a plan is REBUILT in the same thread, which is exactly what a client
    editing a plan-card line causes. Comparing the stored plans is deterministic and
    needs no new UI: whatever they changed, the next build carries it.

    Only tracked fields are compared. A regenerated image changes bytes on every
    build, so `creative` compares the URL rather than the creative object, which would
    otherwise report a modification on every rebuild and drown the real signal.
    """
    if not previous or not current:
        return []
    prev_plan, prev_req = previous.get("plan") or {}, previous.get("req") or {}
    cur_plan, cur_req = current.get("plan") or {}, current.get("req") or {}

    changes: list[dict] = []
    for field, read in _TRACKED_FIELDS.items():
        try:
            before, after = read(prev_plan, prev_req), read(cur_plan, cur_req)
        except Exception:
            continue
        if before != after:
            changes.append({
                "field": field,
                "from": before,
                "to": after,
                "changed_at": _now(),
            })
    return changes


async def record_plan_revision(
    db, *, thread_id: str, brand_id: str, previous: dict, current: dict,
    regeneration_reason: str = "",
) -> None:
    """Accumulate this build's changes onto the thread, for the launch to collect.

    Kept on the thread rather than the plan because a plan id changes on every
    rebuild — the thread is the only thing that survives the whole conversation.
    """
    if db is None or not thread_id:
        return
    changes = diff_plans(previous, current)
    if not changes and not regeneration_reason:
        return
    try:
        update: dict = {"$setOnInsert": {"thread_id": thread_id, "brand_id": brand_id}}
        if changes:
            update["$push"] = {"modifications": {"$each": changes}}
        if regeneration_reason:
            update.setdefault("$push", {})["regeneration_reasons"] = regeneration_reason
        await db[REVISIONS].update_one({"thread_id": thread_id}, update, upsert=True)
    except Exception as e:
        print(f"[CampaignRecord] revision not recorded for {thread_id}: {e}", flush=True)


async def _load_revisions(db, thread_id: str) -> dict:
    if db is None or not thread_id:
        return {}
    try:
        return await db[REVISIONS].find_one({"thread_id": thread_id}, {"_id": 0}) or {}
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


# §2.5. If Jane always picks what has already worked, the data narrows and learning
# stops — the system finds a local optimum in about three months and stays there. A
# share of campaigns is therefore deliberately varied, tagged, and excluded from
# setting defaults while still being measured.
EXPLORATION_SHARE = 0.15


def is_exploration(campaign_id: str, share: float = EXPLORATION_SHARE) -> bool:
    """Whether this campaign is a deliberate variation.

    Derived from the campaign id rather than drawn at random, so the same campaign is
    always classified the same way — a record that flipped between runs would corrupt
    the very comparison the reserve exists to protect. Deterministic, uniform, and
    needs no stored counter.
    """
    if not campaign_id or share <= 0:
        return False
    # A real hash, not a character sum: Meta's campaign ids are sequential, so summing
    # their digits clusters hard — measured at 83% selected instead of 15% on 2,000
    # consecutive ids, which would have made most campaigns "exploration" and starved
    # default-setting of the very data it needs.
    digest = hashlib.sha256(str(campaign_id).encode()).digest()
    return (digest[0] << 8 | digest[1]) % 10_000 < int(round(share * 10_000))


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
        revisions = await _load_revisions(db, plan_doc.get("thread_id", ""))
        generated = variant_set.get("variants") or []
        recommended = next((v for v in generated if v.get("recommended")), None)

        record = {
            "campaign_id": campaign_id,
            "brand_id": brand_id,
            "business_id": business_id,
            "created_at": _now(),
            "closed_at": None,
            # Tagged now so it can be excluded from default-setting later while
            # staying in the measurement (§2.5).
            "exploration": is_exploration(campaign_id),

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

            # What the client changed across rebuilds of this campaign (§1.5), and how
            # often the creative was regenerated before they accepted one (§1.3) —
            # both accumulated on the thread, which is the only thing that survives a
            # whole conversation (a plan id changes on every rebuild).
            "modifications": revisions.get("modifications") or [],
            "regeneration_reasons": revisions.get("regeneration_reasons") or [],
            "generated_count": 1 + len(revisions.get("regeneration_reasons") or []),
            # Backfilled from Meta on completion (§1.6) — the half that IS recoverable.
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


# ── Results (§1.6) — the half Meta can give back ─────────────────────────────

async def backfill_results(db, adapter, campaign_id: str) -> dict:
    """Fill a record's `results` from Meta.

    Deliberately the ONLY mutable part of the record. Spend, reach and conversation
    counts can be pulled for any campaign that ever ran, which is precisely why the
    decisions had to be captured live and these did not.

    Safe to re-run: it overwrites `results` and touches nothing else, so a campaign
    still delivering can be refreshed without disturbing what was decided.
    """
    if db is None or not campaign_id or adapter is None:
        return {}
    try:
        record = await db[RECORDS].find_one({"campaign_id": campaign_id}, {"_id": 0, "campaign_id": 1})
        if not record:
            return {}
        summary = await adapter.fetch_campaign_summary(campaign_id)
    except Exception as e:
        print(f"[CampaignRecord] results backfill failed for {campaign_id}: {e}", flush=True)
        return {}

    delivery = (summary.get("delivery") or "").lower()
    results = {
        "spend_ngn": round(float(summary.get("spend_ngn") or 0), 2),
        "reach": summary.get("reach"),
        "impressions": summary.get("impressions"),
        "conversations": summary.get("conversations"),
        "cost_per_conversation_ngn": summary.get("cost_per_conversation_ngn"),
        "ends_at": summary.get("ends_at"),
        "delivery": summary.get("delivery"),
        # "completed normally" means it ran to its end date rather than being stopped.
        "completed_normally": delivery in ("completed", "finished"),
        "pulled_at": _now(),
    }
    try:
        update = {"$set": {"results": results}}
        if results["completed_normally"]:
            update["$set"]["closed_at"] = _now()
        await db[RECORDS].update_one({"campaign_id": campaign_id}, update)
    except Exception as e:
        print(f"[CampaignRecord] results not stored for {campaign_id}: {e}", flush=True)
        return {}
    return results


async def backfill_all_missing(db, adapter, limit: int = 200) -> dict:
    """Pull results for every record that has none yet.

    The retroactive half of §1.6: campaigns that ran before this shipped can still be
    completed from the API. One pass, newest first, and a failure on one record never
    stops the rest — a partially backfilled set is strictly better than none.
    """
    if db is None or adapter is None:
        return {"filled": 0, "failed": 0}
    try:
        cursor = db[RECORDS].find(
            {"$or": [{"results": None}, {"results": {"$exists": False}}]},
            {"_id": 0, "campaign_id": 1},
        ).sort("created_at", -1).limit(limit)
        pending = await cursor.to_list(length=limit)
    except Exception as e:
        print(f"[CampaignRecord] backfill scan failed: {e}", flush=True)
        return {"filled": 0, "failed": 0}

    filled = failed = 0
    for row in pending:
        got = await backfill_results(db, adapter, row.get("campaign_id", ""))
        if got:
            filled += 1
        else:
            failed += 1
    print(f"[CampaignRecord] backfill: {filled} filled, {failed} failed", flush=True)
    return {"filled": filled, "failed": failed}
