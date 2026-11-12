"""
Jane + Ads — the brand's standing ads brief and the checks every plan is held to
(Ads Intelligence PRD §7.1 BR-03/04/06/07, §7.2, AU-03, CA-01/03, BU-03).

The chat captures what THIS campaign is for. Some facts belong to the business and
should not have to be repeated — or be silently forgotten — campaign after campaign:
where it can actually deliver, places it never wants targeted, formats it refuses,
what a good lead and a successful campaign mean to it, and whether it has purchase
tracking at all. They are saved once per brand and every plan is checked against them
before launch.

A check is either a warning (shown on the plan, the client can proceed) or a block
(launch refuses until the plan changes, or — for service area only — the client
explicitly overrides). Nothing here is decided by a model.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

BRIEFS = "jane_ads_brand_briefs"

CONSTRAINTS = {
    "own_photos_only": "Use only my own photos — no AI-generated images",
    "no_ai_video": "No AI-generated video",
    "no_generated_people": "No AI-generated people or faces",
    "no_ugc": "No creator / UGC-style content",
    "no_website": "Don't send people to a website",
}
TRACKING = ("none", "messages_only", "website_pixel", "website_purchases")

TEXT_FIELDS = ("success_definition", "qualified_lead_definition", "customer_data", "assets_available",
               "previous_results")
LIST_FIELDS = ("service_area", "excluded_locations")


def _clean_list(v) -> list[str]:
    if isinstance(v, str):
        v = v.split(",")
    return [str(x).strip()[:80] for x in (v or []) if str(x).strip()][:30]


def normalise(payload: dict) -> dict:
    out: dict = {}
    for k in LIST_FIELDS:
        if k in payload:
            out[k] = _clean_list(payload[k])
    for k in TEXT_FIELDS:
        if k in payload:
            out[k] = str(payload[k] or "").strip()[:600]
    if "constraints" in payload:
        out["constraints"] = sorted({c for c in (payload["constraints"] or []) if c in CONSTRAINTS})
    if "tracking" in payload:
        out["tracking"] = payload["tracking"] if payload["tracking"] in TRACKING else "none"
    return out


async def load(db, brand_id: str) -> dict:
    doc = await db[BRIEFS].find_one({"brand_id": brand_id}, {"_id": 0}) or {}
    return {"brand_id": brand_id, "service_area": [], "excluded_locations": [], "constraints": [],
            "tracking": "none", **{k: "" for k in TEXT_FIELDS}, **doc}


async def save(db, brand_id: str, payload: dict, actor: str) -> dict:
    await db[BRIEFS].update_one({"brand_id": brand_id}, {"$set": {
        **normalise(payload), "brand_id": brand_id, "updated_by": actor,
        "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    return await load(db, brand_id)


# ── Checks ────────────────────────────────────────────────────────────────────

def _matches(name: str, places: list[str]) -> bool:
    n = name.lower().strip()
    return any(p.lower() in n or n in p.lower() for p in places if p.strip())


def targeted_places(plan: dict) -> list[str]:
    geo = plan.get("geo") or {}
    names = [p.get("name", "") for p in geo.get("pins") or [] if p.get("name")]
    if not names and geo.get("fallback_area"):
        names = [geo["fallback_area"]]
    if not names and geo.get("city"):
        names = [geo["city"]]
    return names


def _area_checks(plan: dict, brief: dict) -> list[dict]:
    out = []
    geo = plan.get("geo") or {}
    places = targeted_places(plan)
    excluded = [p for p in places if _matches(p, brief.get("excluded_locations") or [])]
    if excluded:
        out.append({"code": "excluded_location", "level": "block",
                    "message": f"This plan targets {', '.join(excluded)}, which you've excluded. "
                               "Remove it from Locations before launching."})
    area = brief.get("service_area") or []
    if not area:
        return out
    if not places and geo.get("mode") == "non_local":
        out.append({"code": "outside_service_area", "level": "block", "overridable": True,
                    "message": f"This plan targets the whole country, but you serve {', '.join(area)}. "
                               "Narrow the locations, or confirm you can deliver everywhere."})
        return out
    city_ok = bool(geo.get("city")) and _matches(geo["city"], area)
    outside = [p for p in places if not (_matches(p, area) or city_ok) and p not in excluded]
    if outside:
        out.append({"code": "outside_service_area", "level": "block", "overridable": True,
                    "message": f"{', '.join(outside)} may be outside where you deliver "
                               f"({', '.join(area)}). Change the locations, or confirm you serve them."})
    return out


def _constraint_checks(plan: dict, brief: dict) -> list[dict]:
    out = []
    rules = set(brief.get("constraints") or [])
    creative = plan.get("creative") or {}
    if "own_photos_only" in rules and creative.get("source") == "generate":
        out.append({"code": "constraint_own_photos", "level": "block",
                    "message": "You asked for your own photos only, but this ad's image was generated. "
                               "Upload one of your photos as the creative."})
    if "no_website" in rules and plan.get("destination_type") in ("website", "custom"):
        out.append({"code": "constraint_no_website", "level": "block",
                    "message": "You asked not to send people to a website. Switch the ad destination "
                               "to WhatsApp or Instagram DMs."})
    return out


def optimisation_note(plan: dict, brief: dict) -> Optional[dict]:
    """What Meta is really told to find, said plainly when it is a stand-in for the
    outcome the client wants (PRD §7.2)."""
    objective = (plan.get("objective") or "").lower()
    dest = plan.get("destination_type") or ""
    tracking = brief.get("tracking") or "none"
    if objective == "sales" and dest in ("website", "custom") and tracking != "website_purchases":
        return {"code": "proxy_optimisation", "level": "warning",
                "message": "Meta will look for people likely to click your link, not buyers: purchase "
                           "tracking isn't connected to your website. Sales are only known when you "
                           "record them."}
    if objective in ("sales", "leads") and dest in ("whatsapp", "instagram_dm", "messenger"):
        return {"code": "proxy_optimisation", "level": "warning",
                "message": "Meta will look for people likely to start a conversation. Whether those "
                           "become orders is only known when you mark leads Converted in Customer Messages."}
    return None


def structure(plan: dict, req: dict, sibling_plans: int = 1) -> dict:
    """The campaign → ad set → ad layout this plan launches as, with its reason."""
    from .growth.stages import recommend_structure
    platforms = plan.get("platforms") or [{}]
    budget = float(req.get("budget_ngn") or sum(float(p.get("budget_ngn") or 0) for p in platforms))
    days = max(int(p.get("days") or 1) for p in platforms)
    daily = budget / days if days else budget
    rec = recommend_structure(daily, days)
    n = 1 + len(plan.get("concepts") or [])
    split = n > 1 and plan.get("ad_layout") == "abo"
    out = {"campaigns": 1, "ad_sets": n if split else 1, "ads_per_ad_set": 1 if split else n,
           "budget_type": "daily, on the ad set",
           "daily_ngn": round(daily, 2), "days": days, "total_ngn": budget,
           "why": "One campaign, one ad set and one ad keeps the whole budget learning in one place.",
           "recommended": rec, "warnings": []}
    if split:
        out.update({"per_ad_set_daily_ngn": round(daily / n, 2),
                    "why": f"{n} different ads, each in its own ad set with an equal ₦{daily / n:,.0f} "
                           "a day, so each gets a fair chance before any is judged."})
    elif n > 1:
        out["why"] = (f"{n} different ads share one ad set and its budget. Meta shows them to the same "
                      "audience and leans toward whichever is doing better, so it can favour one early.")
    if sibling_plans > 1:
        out["warnings"].append({
            "code": "split_budget", "level": "warning",
            "message": f"You picked {sibling_plans} audience plans. Each launches as its own campaign "
                       f"with its own budget, so the total is {sibling_plans}× this one — and each "
                       "gets less evidence than one combined campaign would."})
    if rec.get("max_by_daily_floor", 1) < sibling_plans:
        out["warnings"].append({
            "code": "budget_too_thin", "level": "warning",
            "message": f"At ₦{daily:,.0f} a day, the budget comfortably supports "
                       f"{rec['max_by_daily_floor']} ad set{'s' if rec['max_by_daily_floor'] > 1 else ''}; "
                       "splitting further spreads it too thin to learn."})
    return out


def costs(req: dict, due_ngn: float, production_ngn: float = 0.0) -> dict:
    """Media spend, URI's fee and creative production, kept apart (PRD BU-03)."""
    spend = float(req.get("budget_ngn") or 0)
    return {"currency": "NGN", "ad_spend_ngn": spend, "uri_fee_ngn": round(max(0.0, due_ngn - spend), 2),
            "creative_production_ngn": round(production_ngn, 2),
            "total_ngn": round(due_ngn + production_ngn, 2),
            "note": "Ad spend goes to the ad platform; the fee and production are URI's charges."}


def check_plan(plan: dict, req: dict, brief: dict, sibling_plans: int = 1) -> dict:
    from .creative_paths import checks as path_checks
    issues = _area_checks(plan, brief)
    # Every ad being tested answers to the brief, not just the first.
    seen = set()
    for ad in [plan.get("creative") or {}, *(plan.get("concepts") or [])]:
        one = {**plan, "creative": ad}
        for issue in _constraint_checks(one, brief) + path_checks(one, brief):
            if (issue["code"], issue["message"]) not in seen:
                seen.add((issue["code"], issue["message"]))
                issues.append(issue)
    note = optimisation_note(plan, brief)
    if note:
        issues.append(note)
    layout = structure(plan, req, sibling_plans)
    issues += layout["warnings"]
    missing = [label for key, label in (("service_area", "where you deliver"),
                                        ("success_definition", "what success means"),
                                        ("qualified_lead_definition", "what makes a good lead"))
               if not brief.get(key)]
    return {"issues": issues, "structure": layout,
            "blocking": [i for i in issues if i["level"] == "block"],
            "brief_missing": missing,
            "confidence": "lower" if len(missing) >= 2 else "normal"}


def unresolved_blocks(checks: dict, allow_out_of_area: bool) -> list[dict]:
    return [i for i in checks["blocking"] if not (allow_out_of_area and i.get("overridable"))]


# ── Guided / Advanced mode (PRD §8) ───────────────────────────────────────────
# How much of the machinery a user sees — never what is decided or charged. A mode
# is per user per brand, and switching it changes no plan, campaign or setting.

MODES_COLLECTION = "jane_ads_modes"
MODES = ("guided", "advanced")


async def load_mode(db, brand_id: str, user_id: str) -> str:
    doc = await db[MODES_COLLECTION].find_one({"brand_id": brand_id, "user_id": user_id}, {"mode": 1})
    return (doc or {}).get("mode") or "guided"


async def save_mode(db, brand_id: str, user_id: str, mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    await db[MODES_COLLECTION].update_one(
        {"brand_id": brand_id, "user_id": user_id},
        {"$set": {"mode": mode, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    return mode
