"""
Product metrics (PRD §17): "share of insights rated useful or acted on,
approval and dismissal reasons, content created from insights and first-
draft approval, false/duplicate alert rates, cost per qualified lead for
listening-informed ad tests versus baseline."

Built from mi_listen_cards/mi_listen_card_actions, plus two real joins added
once the data to join against existed (Phase B wired Create/ad-test; this
module never had its own):
- first_draft_approval_rate joins mi_listen_cards.created_draft_ids against
  content_drafts.status (that module's own, pre-existing approval lifecycle
  — listening never duplicates or re-decides it, only reads the outcome).
- ad_test_cost_per_qualified_lead joins an ad_test card's content_id (the
  Jane Ads thread_id it opened) against any jane_ads_meta_campaigns actually
  launched from that thread, then reuses jane_ads' own, already-correct
  spend/lead-stage aggregation (growth.metrics, growth.leads) rather than
  recomputing it here. Most ad_test cards will have no launched campaign yet
  — a draft thread opening is not a launch, and nothing here assumes it
  became one; "not yet available" names exactly why, not just "no tracking."
No "vs baseline" comparison: nothing tracks a non-listening-informed ad
test's cost-per-lead anywhere in this codebase to compare against, so that
half of the PRD metric is left unreported rather than invented.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

_USEFUL_ACTIONS = {"saved", "acted", "created"}
_CREATED_ACTIONS = {"acted", "created"}

# content_drafts.status values (approval_workflow_service.py) that mean the
# draft was approved as submitted vs. explicitly not approved as its first
# draft. "refined" (sent back for changes) counts as not-approved-first-try
# even if a later revision eventually goes out — that is the honest reading
# of "FIRST-draft approval". Anything else (draft/pending_approval/missing)
# is still undecided and excluded from the rate's denominator.
_DRAFT_APPROVED_STATUSES = {"approved", "ready_to_publish", "scheduled", "publishing", "published"}
_DRAFT_NOT_APPROVED_STATUSES = {"denied", "refined"}


async def category_content_performance(db, cfg: dict, window_days: int = 90,
                                       now: Optional[datetime] = None) -> list[dict]:
    """PRD §11's "Content performance... Category-level priors: which signal
    types and formats work" — de-identified and aggregated across every
    brand (never one brand's own history; that's _relevance_adjustment in
    personalize.py, a completely separate, per-brand mechanism). §15's
    cross-tenant safeguard applies directly: "cross-tenant learning uses
    aggregated, de-identified signals only, with minimum-group thresholds"
    — a (category, signal type) group below that threshold is left out
    entirely, never shown with an n too small to mean anything.

    Reporting only, not yet a live scoring input: wiring this into
    personalize.score() would be exactly the kind of change this module's
    own design principles (§3: "Measured, not assumed... compared in shadow
    mode") say needs validating against real production behaviour first —
    shadow.py already exists for that and is the right next step, not a
    silent, unvalidated relevance bias shipped alongside this report."""
    min_samples = cfg.get("learning", {}).get("category_prior_min_samples", 5)
    now = now or datetime.utcnow()
    since = now - timedelta(days=window_days)

    cards = [c async for c in db["mi_listen_cards"].find(
        {"created_at": {"$gte": since}, "created_draft_ids": {"$exists": True, "$ne": []}},
        {"market": 1, "type": 1, "created_draft_ids": 1},
    )]
    if not cards:
        return []

    draft_to_group: dict[str, tuple[str, str]] = {}
    for c in cards:
        category_id = (c.get("market") or "").split(":", 1)[0]
        if not category_id:
            continue
        for d in c.get("created_draft_ids") or []:
            draft_to_group[d] = (category_id, c.get("type") or "unknown")

    if not draft_to_group:
        return []

    groups: dict[tuple[str, str], list[float]] = {}
    async for d in db["content_drafts"].find(
        {"id": {"$in": list(draft_to_group.keys())}, "engagement_rate": {"$ne": None}},
        {"id": 1, "engagement_rate": 1},
    ):
        group = draft_to_group.get(d["id"])
        if group is None:
            continue
        rate = d.get("engagement_rate")
        if rate is None:
            continue
        groups.setdefault(group, []).append(float(rate))

    out = []
    for (category_id, signal_type), rates in sorted(groups.items()):
        if len(rates) < min_samples:
            continue
        out.append({
            "category_id": category_id, "signal_type": signal_type, "samples": len(rates),
            "avg_engagement_rate": round(sum(rates) / len(rates), 3),
        })
    return sorted(out, key=lambda r: -r["avg_engagement_rate"])


async def product_metrics(db, market: Optional[str] = None, window_days: int = 30,
                          now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(days=window_days)

    card_q: dict = {"created_at": {"$gte": since}}
    if market:
        card_q["market"] = market
    cards = [c async for c in db["mi_listen_cards"].find(card_q)]
    cards_shown = len(cards)
    if cards_shown == 0:
        return {"window_days": window_days, "cards_shown": 0,
                "not_available": {"first_draft_approval_rate": "no cards shown in this window",
                                  "ad_test_cost_per_qualified_lead": "no cards shown in this window"}}

    action_q: dict = {"at": {"$gte": since}}
    if market:
        action_q["market"] = market
    latest_action: dict[str, dict] = {}
    async for a in db["mi_listen_card_actions"].find(action_q).sort([("at", 1)]):
        latest_action[a["card_id"]] = a  # later writes overwrite earlier ones per card, in time order

    useful = sum(1 for a in latest_action.values() if a["action"] in _USEFUL_ACTIONS)
    dismissed = [a for a in latest_action.values() if a["action"] == "dismissed"]
    created = sum(1 for a in latest_action.values() if a["action"] in _CREATED_ACTIONS)

    not_available: dict[str, str] = {}
    first_draft = await _first_draft_approval_rate(db, cards)
    if first_draft is None:
        not_available["first_draft_approval_rate"] = (
            "no listening-created draft has been approved or rejected yet in this window")

    ad_test = await _ad_test_cost_per_qualified_lead(db, cards)
    if ad_test is None:
        not_available["ad_test_cost_per_qualified_lead"] = (
            "no ad test opened from a Listening card has a launched campaign with spend yet")

    out = {
        "window_days": window_days, "cards_shown": cards_shown,
        "useful_or_acted_on_rate": round(useful / cards_shown, 3),
        "dismissal_rate": round(len(dismissed) / cards_shown, 3),
        "dismissal_reasons": dict(Counter(a.get("reason") or "no reason given" for a in dismissed).most_common(10)),
        "content_created_count": created,
        "content_created_rate": round(created / cards_shown, 3),
    }
    if first_draft is not None:
        out["first_draft_approval_rate"] = first_draft
    if ad_test is not None:
        out["ad_test_cost_per_qualified_lead"] = ad_test
    if not_available:
        out["not_available"] = not_available
    return out


async def _first_draft_approval_rate(db, cards: list[dict]) -> Optional[dict]:
    draft_ids = sorted({d for c in cards for d in (c.get("created_draft_ids") or []) if d})
    if not draft_ids:
        return None
    statuses: dict[str, int] = Counter()
    async for d in db["content_drafts"].find({"id": {"$in": draft_ids}}, {"status": 1}):
        statuses[d.get("status") or "unknown"] += 1
    approved = sum(n for s, n in statuses.items() if s in _DRAFT_APPROVED_STATUSES)
    not_approved = sum(n for s, n in statuses.items() if s in _DRAFT_NOT_APPROVED_STATUSES)
    decided = approved + not_approved
    if decided == 0:
        return None
    return {
        "drafts_created": len(draft_ids), "drafts_decided": decided, "drafts_approved": approved,
        "drafts_pending": len(draft_ids) - decided, "rate": round(approved / decided, 3),
    }


async def _ad_test_cost_per_qualified_lead(db, cards: list[dict]) -> Optional[dict]:
    threads = [(c.get("content_id"), c.get("brand_id")) for c in cards
              if (c.get("action") or {}).get("type") == "ad_test" and c.get("content_id")]
    if not threads:
        return None
    try:
        from app.agents.jane_ads.growth import leads as jane_leads
        from app.agents.jane_ads.growth.detect import totals_for_ui
        from app.agents.jane_ads.growth.metrics import load as load_jane_metrics
    except Exception:
        return None  # jane_ads growth tooling unavailable in this environment

    total_spend = total_qualified = total_leads = 0.0
    campaigns_counted = 0
    for thread_id, brand_id in threads:
        if not (thread_id and brand_id):
            continue
        try:
            campaign = await db["jane_ads_meta_campaigns"].find_one(
                {"brand_id": brand_id, "thread_id": thread_id}, {"campaign_id": 1})
            if not campaign or not campaign.get("campaign_id"):
                continue
            cid = campaign["campaign_id"]
            rows = await load_jane_metrics(db, cid)
            if not rows:
                continue
            spend = float(totals_for_ui(rows).get("spend_ngn") or 0)
            ad_rows = await load_jane_metrics(db, cid, level="ad")
            ad_ids = sorted({r["ad_id"] for r in ad_rows if r.get("ad_id")})
            stats = await jane_leads.stats(db, brand_id, ad_ids, spend)
        except Exception as e:
            print(f"[MI][listen] ad-test metrics join skipped for thread {thread_id}: {e}")
            continue
        campaigns_counted += 1
        total_spend += spend
        total_leads += stats.get("leads") or 0
        total_qualified += (stats["by_stage"]["qualified"] + stats["by_stage"]["converted"]) if stats.get("by_stage") else 0

    if campaigns_counted == 0 or total_spend <= 0:
        return None
    return {
        "campaigns_counted": campaigns_counted, "total_spend_ngn": round(total_spend, 2),
        "total_leads": int(total_leads), "total_qualified_leads": int(total_qualified),
        "cost_per_qualified_lead_ngn": round(total_spend / total_qualified, 2) if total_qualified else None,
        "note": "" if total_qualified else "Leads exist but none marked Qualified/Converted yet in Customer Messages.",
    }
