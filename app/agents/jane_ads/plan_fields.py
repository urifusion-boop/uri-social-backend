"""
Jane + Ads — the review step: the plan as a list of fields a client can edit.

Between "here is the plan" and "launch it" there was nothing but a button. Everything
Jane decided — who it targets, where, what it says — was take-it-or-leave-it, and the
only way to change one line was to argue with her in prose via /ask and hope the whole
plan came back the same except the bit you meant.

This module turns the plan into named, typed, individually editable fields, and takes
edits back the same way. The client reads a line, changes it, saves, and THAT is what
launches.

The one rule that makes this safe: **an edit is validated against the same machinery
the launch uses, at the moment it is made.** A location must resolve to a Meta named
location, an interest must survive Meta's own validation, ad copy must pass the policy
scan. Anything that would fail at launch is rejected HERE, with a reason, while the
client is still looking at it — instead of being accepted politely and then blowing up
after the wallet has been debited.

That direction matters. Accepting an edit we cannot honour is the same class of bug as
a plan card promising a location the ad set never targeted: the system says yes and
then does something else.
"""
from __future__ import annotations

from typing import Any, Optional

from . import constants as C
from .objectives import CHOICES as OBJECTIVE_CHOICES
from .models import CampaignPlan, CampaignRequest, GeoMode, GeoPin, GeoPlan, PinSource, Platform, PlatformPlan

# Meta's own bounds. Sending outside these fails the ad set create outright.
MIN_AGE, MAX_AGE = 18, 65
_GENDER_TO_CODES = {"all": [], "men": [1], "women": [2]}
_CODES_TO_GENDER = {(): "all", (1,): "men", (2,): "women"}

# Locations, per geo.py: Meta names at most this many and more than three pockets
# stops being targeting and starts being a shape drawn around a city.
MAX_LOCATIONS = 3
MAX_INTERESTS = 6

# Where the ad is allowed to show. Leaving publisher_platforms unset is Meta's
# "automatic" — Facebook, Instagram, Audience Network and Messenger — which is the
# best-delivering default but also the one that spends on Audience Network, where
# lifetime numbers put the cost per impression far above Facebook's. A client who
# only wants Instagram must be able to say so.
_PLACEMENTS = {
    "automatic": None,
    "facebook_and_instagram": ["facebook", "instagram"],
    "instagram_only": ["instagram"],
    "facebook_only": ["facebook"],
}
_PLACEMENT_LABELS = {
    "automatic": "Let Meta choose (includes Audience Network)",
    "facebook_and_instagram": "Facebook and Instagram",
    "instagram_only": "Instagram only",
    "facebook_only": "Facebook only",
}


def _gender_of(targeting: dict) -> str:
    return _CODES_TO_GENDER.get(tuple(targeting.get("genders") or []), "all")


def _placement_of(targeting: dict) -> str:
    current = targeting.get("publisher_platforms")
    if not current:
        return "automatic"
    for key, value in _PLACEMENTS.items():
        if value and sorted(value) == sorted(current):
            return key
    return "automatic"


def _daily_floor_for(platform: Optional[PlatformPlan]) -> float:
    """The real daily-spend floor for whichever platform this plan is on — was
    hardcoded to Meta's own floor everywhere in this module (both the help text
    and, more seriously, the budget/duration/daily-spend validation itself),
    which is wrong for TikTok's real ₦31,000 floor.

    Mirrors decision_engine.py's own _days_for exactly, so this module and the
    decision engine can never disagree about what "too thin to run" means for a
    given platform: Meta uses MIN_DAILY_SPEND_NGN (₦2,000 — Jane's own quality
    floor, deliberately above Meta's raw ₦1,610 platform floor, since a campaign
    sitting on the platform minimum delivers too thinly to learn anything); every
    other platform uses its own real HARD_FLOOR_DAILY_NGN, since Jane has no
    equivalent quality-floor policy defined for TikTok/Google yet and TikTok's
    real floor (₦31,000) is already far above any reasonable quality floor
    anyway. Google's is 0 (CPC-driven, no hard floor) — deliberately preserved
    as 0 here rather than falling back to Meta's number, so a Google-only plan's
    budget/days validation stays unconstrained, matching decision_engine.py."""
    if not platform:
        return C.MIN_DAILY_SPEND_NGN
    if platform.platform == Platform.META:
        return max(C.HARD_FLOOR_DAILY_NGN.get("meta", 0), C.MIN_DAILY_SPEND_NGN)
    return C.HARD_FLOOR_DAILY_NGN.get(platform.platform.value, C.MIN_DAILY_SPEND_NGN)


def _interest_names(targeting: dict) -> list[str]:
    """The interest/behaviour labels out of Meta's flexible_spec.

    Interests and behaviours share ONE flexible_spec entry so Meta ORs them (two
    entries would AND them, which is a different and much smaller audience), so this
    walks every key of every entry rather than assuming a shape.
    """
    names: list[str] = []
    for entry in targeting.get("flexible_spec") or []:
        for value in entry.values():
            for item in value or []:
                name = (item or {}).get("name")
                if name and name not in names:
                    names.append(name)
    return names


def describe(plan: CampaignPlan, req: CampaignRequest) -> list[dict[str, Any]]:
    """The plan as editable lines, in the order a client reads them.

    `editable: False` lines are shown but not changeable — they are derived from other
    fields (the daily split) or fixed by the connection (the Page), and pretending
    otherwise would invite an edit we would have to silently ignore.

    Live-caught 2026-09-23, revised 2026-09-25: every targeting-shaped field here
    used to render unconditionally, Meta-labelled, on a TikTok plan too — but
    adapters/tiktok.py didn't read plan.geo or plan.audience_targeting at all when
    it built the ad group, so editing any of them on a TikTok plan changed nothing
    real ("system says yes and does something else", the exact bug class this
    module's docstring says it exists to prevent). adapters/tiktok.py now genuinely
    uses location/gender/age (confirmed against TikTok's own Audience targeting and
    Enumerations API reference), so locations/gender/age are real again for both
    platforms. Interests and placement stay Meta-only: interests have no TikTok
    mapping wired up yet (deferred, not silently dropped — see
    _resolve_tiktok_locations's own docstring in adapters/tiktok.py for the same
    honesty pattern), and placement ("Facebook and Instagram" vs "Instagram only"
    etc.) is a Meta-only concept with no TikTok equivalent at all.
    """
    targeting = plan.audience_targeting or {}
    creative = plan.creative
    platform = plan.platforms[0] if plan.platforms else None
    geo = plan.geo
    is_tiktok = bool(platform and platform.platform == Platform.TIKTOK)
    floor = _daily_floor_for(platform)
    platform_name = "TikTok" if is_tiktok else "Meta"

    fields: list[dict[str, Any]] = [
        {
            "key": "objective", "label": "Goal", "type": "select",
            "value": (plan.objective.value if plan.objective else ""),
            "options": [c["value"] for c in OBJECTIVE_CHOICES],
            "option_labels": {c["value"]: c["label"] for c in OBJECTIVE_CHOICES},
            "editable": True,
            "help": "What Meta optimises toward. The last chance to change it — Meta "
                    "refuses to alter a campaign's objective once it has launched.",
        },
        {
            "key": "headline", "label": "Headline", "type": "text",
            "value": (creative.headline if creative else ""),
            "editable": True, "max_length": 40,
            "help": "The bold line. Around five words works best.",
        },
        {
            "key": "caption", "label": "Caption", "type": "textarea",
            "value": (creative.primary_text if creative else ""),
            "editable": True, "max_length": 500,
            "help": "The body of the ad — what the reader actually reads.",
        },
    ]

    fields += [
        {
            "key": "locations", "label": "Locations", "type": "list",
            "value": [p.name for p in (geo.pins if geo else [])],
            "editable": True, "max_items": MAX_LOCATIONS,
            "help": (f"Up to {MAX_LOCATIONS} areas. Each must be somewhere TikTok can name — "
                     "we never target raw coordinates."
                     if is_tiktok else
                     f"Up to {MAX_LOCATIONS} areas. Each must be somewhere Meta can name — "
                     "we never target raw coordinates."),
        },
    ]
    if not is_tiktok:
        # Meta-only: no TikTok mapping wired up yet for interests, and placement
        # ("Facebook and Instagram" vs "Instagram only") has no TikTok equivalent
        # at all — TikTok only ever runs on TikTok's own placement.
        fields += [
            {
                "key": "interests", "label": "Interests and behaviours", "type": "list",
                "value": _interest_names(targeting),
                "editable": True, "max_items": MAX_INTERESTS,
                "help": "Checked against Meta's own targeting catalogue when you save.",
            },
            {
                "key": "placement", "label": "Where it shows", "type": "select",
                "value": _placement_of(targeting),
                "options": list(_PLACEMENTS.keys()),
                "option_labels": _PLACEMENT_LABELS,
                "editable": True,
                "help": "Automatic delivers best but also spends on Audience Network. "
                        "Pick a platform to keep it off everything else.",
            },
        ]
    age_min_field: dict[str, Any] = {
        "key": "age_min", "label": "Minimum age", "type": "number",
        "value": targeting.get("age_min", MIN_AGE),
        "min": MIN_AGE, "max": MAX_AGE, "editable": True,
    }
    if is_tiktok:
        age_min_field["help"] = (
            "TikTok targets in age bands, not this exact number — the range you give "
            "gets mapped onto whichever of its bands overlap it."
        )
    fields += [
        {
            "key": "gender", "label": "Gender", "type": "select",
            "value": _gender_of(targeting),
            "options": ["all", "men", "women"], "editable": True,
            "help": "Who sees it. 'All' is usually right unless the product is gendered.",
        },
        age_min_field,
        {
            "key": "age_max", "label": "Maximum age", "type": "number",
            "value": targeting.get("age_max", MAX_AGE),
            "min": MIN_AGE, "max": MAX_AGE, "editable": True,
        },
    ]

    fields += [
        {
            "key": "budget_ngn", "label": "Budget", "type": "number",
            "value": req.budget_ngn, "min": 1, "editable": True, "prefix": "₦",
            "help": "The total you're spending on this campaign.",
        },
        {
            "key": "days", "label": "Duration (days)", "type": "number",
            "value": (platform.days if platform else 0),
            "min": 1, "max": 90, "editable": True,
            "help": "However long you want — Jane's default is only a starting point. "
                    f"Shorter means more spend per day, and the daily figure cannot go "
                    f"below ₦{floor:,.0f} on {platform_name}.",
        },
        {
            "key": "daily_spend", "label": "Daily spend", "type": "number",
            "value": round(req.budget_ngn / platform.days, 2) if platform and platform.days else None,
            "min": floor, "editable": True, "prefix": "₦",
            "help": f"How much goes out each day. At least ₦{floor:,.0f} on {platform_name} — "
                    f"below that an ad delivers too thinly to learn anything (or, on TikTok, "
                    f"is under the platform's own floor). Setting this changes how many days "
                    f"the budget lasts.",
        },
        {
            "key": "destination", "label": "Where taps go", "type": "derived",
            "value": plan.destination_link or plan.destination_type,
            "editable": False,
            "help": "Set by your connection, not by this plan.",
        },
    ]
    return fields


async def _validated_locations(
    names: list[str], region: str, access_token: str
) -> tuple[list[GeoPin], list[str]]:
    """Keep only what Meta can name, and say why the rest went.

    Deliberately strict: a location we cannot name is one we cannot target, so
    accepting it here would mean showing the client a plan line that the launch then
    silently discards.
    """
    from .geo_names import resolve_named_location

    pins: list[GeoPin] = []
    rejected: list[str] = []
    for raw in names[:MAX_LOCATIONS]:
        name = (raw or "").strip()
        if not name:
            continue
        try:
            hit = await resolve_named_location(name, region, access_token)
        except Exception as e:
            print(f"[PlanFields] location lookup failed for {name!r}: {e}", flush=True)
            hit = None
        if hit:
            pins.append(GeoPin(name=hit["name"], source=PinSource.GEOCODED,
                               reason="chosen by you"))
        else:
            rejected.append(
                f"{name} — Meta has no targetable area by that name in {region or 'this region'}"
            )
    return pins, rejected


def _other_flex_fields(targeting: dict) -> dict[str, list]:
    """Everything in flexible_spec that is not an interest.

    Meta files each targeting type under its own key and rejects an id placed under the
    wrong one, so these travel alongside the interests rather than merged into them.
    """
    other: dict[str, list] = {}
    for entry in targeting.get("flexible_spec") or []:
        for key, value in entry.items():
            if key == "interests" or not value:
                continue
            other.setdefault(key, []).extend(value)
    return other


def _resolved_interests(targeting: dict) -> dict[str, dict]:
    """The interests already on the plan, keyed by the name the UI displays."""
    found: dict[str, dict] = {}
    for entry in targeting.get("flexible_spec") or []:
        for value in entry.values():
            for item in value or []:
                name = (item or {}).get("name")
                if name and item.get("id"):
                    found[name.strip().lower()] = {"id": item["id"], "name": name}
    return found


async def _validated_interests(
    names: list[str], access_token: str, already: Optional[dict[str, dict]] = None
) -> tuple[list[dict], list[str]]:
    """Resolve each label to a real Meta interest id, dropping what does not exist.

    Anything already on the plan is kept by its stored id WITHOUT re-searching. Meta
    hands back display names carrying their category — "Marketing (business and
    finance)" — and its own search cannot find that string again, so re-resolving an
    untouched interest would delete it. Live-caught: a client edited one field and the
    save reported five interests they had never touched as untargetable.
    """
    import httpx

    from app.core.config import settings
    from .audience_targeting import _resolve_interest

    already = already or {}
    kept: list[dict] = []
    rejected: list[str] = []
    graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    async with httpx.AsyncClient(timeout=15) as client:
        for raw in names[:MAX_INTERESTS]:
            name = (raw or "").strip()
            if not name:
                continue
            existing = already.get(name.lower())
            if existing:
                kept.append(existing)
                continue
            try:
                hit = await _resolve_interest(client, graph_base, access_token, name)
            except Exception as e:
                print(f"[PlanFields] interest lookup failed for {name!r}: {e}", flush=True)
                hit = None
            if hit:
                kept.append({"id": hit["id"], "name": hit["name"]})
            else:
                rejected.append(f"{name} — not something Meta lets you target")
    return kept, rejected


async def apply_edits(
    plan: CampaignPlan,
    req: CampaignRequest,
    edits: dict[str, Any],
    access_token: str = "",
    *,
    tiktok_advertiser_id: str = "",
    tiktok_access_token: str = "",
) -> tuple[CampaignPlan, CampaignRequest, list[str], list[str]]:
    """Fold validated edits into the plan.

    Returns (plan, req, applied_keys, rejections). Rejections never block the rest:
    a bad interest does not throw away a good caption, because losing typed work is
    its own bug. The caller decides what to tell the client.
    """
    from .policy import Severity, review_ad_creative

    applied: list[str] = []
    rejections: list[str] = []
    targeting = dict(plan.audience_targeting or {})
    plan_update: dict[str, Any] = {}
    req_update: dict[str, Any] = {}
    platform = plan.platforms[0] if plan.platforms else None
    is_tiktok = bool(platform and platform.platform == Platform.TIKTOK)
    platform_name = "TikTok" if is_tiktok else "Meta"

    # ── objective ─────────────────────────────────────────────────────────────
    # Before launch ONLY. Meta refuses to change a campaign's objective once it has an
    # ad set — "Updating objective of non-empty campaign" — and every launched campaign
    # has one, so this really is the last moment it can move. Probed live.
    if "objective" in edits:
        from .objectives import coerce as coerce_objective

        picked = coerce_objective(str(edits.get("objective") or ""))
        allowed = {c["value"] for c in OBJECTIVE_CHOICES}
        if not picked or picked.value not in allowed:
            rejections.append(
                "That is not a goal we can run — pick one of: "
                + ", ".join(c["label"] for c in OBJECTIVE_CHOICES) + "."
            )
        else:
            plan_update["objective"] = picked
            plan_update["platforms"] = [
                pl.model_copy(update={"objective": picked}) for pl in plan.platforms
            ]
            applied.append("objective")

    # ── ad copy ───────────────────────────────────────────────────────────────
    if plan.creative is not None and ({"headline", "caption"} & edits.keys()):
        headline = str(edits.get("headline", plan.creative.headline) or "").strip()
        caption = str(edits.get("caption", plan.creative.primary_text) or "").strip()
        if not headline or not caption:
            rejections.append("Headline and caption cannot be empty.")
        else:
            verdict = review_ad_creative(headline, caption, getattr(plan.creative, "image_prompt", "") or "")
            blocking = [v for v in verdict.violations if v.severity == Severity.BLOCK]
            if blocking:
                rejections.append(
                    "Ad copy rejected: " + "; ".join(v.guidance for v in blocking)
                )
            else:
                plan_update["creative"] = plan.creative.model_copy(
                    update={"headline": headline, "primary_text": caption}
                )
                applied += [k for k in ("headline", "caption") if k in edits]

    # ── locations ─────────────────────────────────────────────────────────────
    # Real for both platforms now (2026-09-25) — TikTok resolves against its own
    # /tool/region/ (adapters/tiktok.py's _resolve_tiktok_locations) instead of
    # Meta's Graph API, but both land in the same plan.geo.pins shape, since
    # that's what each platform's own launch step re-resolves by name from.
    if "locations" in edits:
        names = [str(n) for n in (edits.get("locations") or [])]
        if is_tiktok:
            if not (tiktok_advertiser_id and tiktok_access_token):
                rejections.append("TikTok isn't configured, so locations can't be checked right now.")
            else:
                from .adapters.tiktok import _resolve_tiktok_locations

                hits, bad = await _resolve_tiktok_locations(
                    names[:MAX_LOCATIONS], tiktok_advertiser_id, tiktok_access_token)
                rejections += [f"{n} — TikTok has no targetable area by that name in Nigeria" for n in bad]
                pins = [GeoPin(name=h["name"], source=PinSource.GEOCODED, reason="chosen by you") for h in hits]
                if pins:
                    base = plan.geo or GeoPlan(mode=GeoMode.OWN_RADIUS, city=(plan.geo.city if plan.geo else req.geo))
                    plan_update["geo"] = base.model_copy(update={"pins": pins})
                    applied.append("locations")
                elif names:
                    rejections.append("No location was changed — none of those could be named by TikTok.")
        else:
            region = (plan.geo.city if plan.geo else "") or req.geo
            pins, bad = await _validated_locations(names, region, access_token)
            rejections += bad
            if pins:
                base = plan.geo or GeoPlan(mode=GeoMode.OWN_RADIUS, city=region)
                plan_update["geo"] = base.model_copy(update={"pins": pins})
                applied.append("locations")
            elif names:
                rejections.append("No location was changed — none of those could be named by Meta.")

    # ── interests, placement ─────────────────────────────────────────────────
    # Meta-only guard: no TikTok mapping wired up yet for interests, and
    # placement has no TikTok equivalent at all. describe() doesn't offer these
    # for a TikTok plan, so this is defence-in-depth for a direct API call, not
    # something the review panel itself can trigger.
    if is_tiktok and ({"interests", "placement"} & edits.keys()):
        rejections.append(
            "TikTok campaigns don't support interest or placement targeting in "
            "Jane yet."
        )
    if not is_tiktok:
        # ── interests ─────────────────────────────────────────────────────────
        if "interests" in edits:
            names = [str(n) for n in (edits.get("interests") or [])]
            if not names:
                targeting.pop("flexible_spec", None)
                applied.append("interests")
            else:
                kept, bad = await _validated_interests(
                    names, access_token, _resolved_interests(plan.audience_targeting or {}))
                rejections += bad
                if kept:
                    # ONE flexible_spec entry: Meta ORs within an entry and ANDs across
                    # entries, and an AND of interests is a near-empty audience.
                    #
                    # Everything in that entry which is NOT an interest — life_events,
                    # behaviors, work_positions, industries — is carried over untouched.
                    # Meta rejects an id filed under the wrong key, so these cannot simply
                    # be folded in with the interests, and dropping them would silently
                    # narrow an audience the client never asked to change. Live-caught on a
                    # real ad set carrying 7 interests and 1 life_event.
                    entry = {k: v for k, v in _other_flex_fields(plan.audience_targeting or {}).items()}
                    entry["interests"] = kept
                    targeting["flexible_spec"] = [entry]
                    applied.append("interests")

        # ── placement ─────────────────────────────────────────────────────────
        if "placement" in edits:
            choice = str(edits.get("placement") or "").strip().lower()
            if choice not in _PLACEMENTS:
                rejections.append(f"Placement must be one of: {', '.join(_PLACEMENTS)}.")
            else:
                placements = _PLACEMENTS[choice]
                if placements:
                    targeting["publisher_platforms"] = placements
                else:
                    targeting.pop("publisher_platforms", None)
                applied.append("placement")

    # ── gender ────────────────────────────────────────────────────────────────
    # Real for both platforms now — TikTok's own gender enum
    # (GENDER_MALE/GENDER_FEMALE/GENDER_UNLIMITED) maps directly onto this same
    # genders encoding (see adapters/tiktok.py's _tiktok_gender_for).
    if "gender" in edits:
        choice = str(edits.get("gender") or "").strip().lower()
        if choice not in _GENDER_TO_CODES:
            rejections.append(f"Gender must be one of: {', '.join(_GENDER_TO_CODES)}.")
        else:
            codes = _GENDER_TO_CODES[choice]
            if codes:
                targeting["genders"] = codes
            else:
                targeting.pop("genders", None)
            applied.append("gender")

    # ── age ───────────────────────────────────────────────────────────────────
    # Real for both platforms now — TikTok maps this same continuous age_min/
    # age_max range onto whichever of its discrete age bands overlap it (see
    # adapters/tiktok.py's _tiktok_age_groups_for), so the bounds validated here
    # stay platform-agnostic rather than needing TikTok's own bucket enum
    # surfaced in the UI.
    if {"age_min", "age_max"} & edits.keys():
        lo = _as_int(edits.get("age_min", targeting.get("age_min", MIN_AGE)))
        hi = _as_int(edits.get("age_max", targeting.get("age_max", MAX_AGE)))
        if lo is None or hi is None:
            rejections.append("Ages must be whole numbers.")
        elif not (MIN_AGE <= lo < hi <= MAX_AGE):
            rejections.append(
                f"Age range must sit between {MIN_AGE} and {MAX_AGE}, with the minimum below the maximum."
            )
        else:
            targeting["age_min"], targeting["age_max"] = lo, hi
            applied += [k for k in ("age_min", "age_max") if k in edits]

    # ── budget, duration and daily spend ────────────────────────────────────────
    # Judged TOGETHER, because what the platform actually rejects is the daily
    # figure they produce between them. Halving the duration (or raising the daily
    # spend directly) is a valid way to clear the floor, so validating any one of
    # the three alone would refuse edits that are in fact fine — and would let a
    # legal-looking combination through that the launch then fails on ("Budget is
    # too low", subcode 1885272 on Meta).
    #
    # Live-caught 2026-09-23: the floor here was hardcoded to a single constant
    # regardless of platform (first C.META_MIN_DAILY_NGN, then, when daily_spend
    # became directly editable in the same window, C.MIN_DAILY_SPEND_NGN) — a
    # TikTok edit could pass this check well under TikTok's real ₦31,000 floor and
    # then fail for real at launch. Now looked up per the plan's own platform via
    # _daily_floor_for, which mirrors decision_engine.py's own logic exactly:
    # Meta uses the ₦2,000 product-quality floor, TikTok its real ₦31,000 floor.
    if {"budget_ngn", "days", "daily_spend"} & edits.keys():
        current_days = plan.platforms[0].days if plan.platforms else C.DEFAULT_CAMPAIGN_DAYS
        budget = _as_float(edits["budget_ngn"]) if "budget_ngn" in edits else req.budget_ngn
        days = _as_int(edits["days"]) if "days" in edits else current_days
        floor = _daily_floor_for(platform)

        # Setting the daily spend decides the DURATION, not the budget: the budget is
        # what the client agreed to pay and is not ours to move on their behalf. An
        # explicit `days` in the same save wins, since they said it outright.
        daily_accepted = False
        if "daily_spend" in edits and "days" not in edits:
            daily = _as_float(edits["daily_spend"])
            if daily is None or daily < floor:
                rejections.append(
                    f"Daily spend must be at least ₦{floor:,.0f} on {platform_name} — below "
                    f"that an ad delivers too thinly to learn anything."
                )
            elif budget is not None and budget > 0:
                days = max(1, round(budget / daily))
                daily_accepted = True

        if "budget_ngn" in edits and (budget is None or budget <= 0):
            rejections.append("Budget must be a number greater than zero.")
        elif "days" in edits and (days is None or not (1 <= days <= 90)):
            rejections.append("Duration must be a whole number of days between 1 and 90.")
        elif budget is not None and plan.per_business_cap_ngn and budget > plan.per_business_cap_ngn:
            rejections.append(
                f"₦{budget:,.0f} is over this brand's cap of ₦{plan.per_business_cap_ngn:,.0f}."
            )
        elif budget is None or days is None:
            rejections.append("Budget and duration must both be numbers.")
        elif floor > 0 and budget / days < floor:
            longest = int(budget // floor)
            rejections.append(
                f"₦{budget:,.0f} over {days} days is ₦{budget / days:,.0f} a day, under "
                f"{platform_name}'s ₦{floor:,.0f} minimum — an ad spending less than that "
                "either delivers too thinly to learn anything or is under the platform's own "
                "floor. "
                + (f"Run it over {longest} days or fewer, or raise the budget."
                   if longest >= 1 else
                   f"You would need at least ₦{floor:,.0f} for a single day.")
            )
        else:
            if "budget_ngn" in edits:
                req_update["budget_ngn"] = budget
                applied.append("budget_ngn")
            if "days" in edits:
                applied.append("days")
            if daily_accepted:
                applied.append("daily_spend")
            if plan.platforms:
                plan_update["platforms"] = [
                    plan.platforms[0].model_copy(update={"budget_ngn": budget, "days": days}),
                    *plan.platforms[1:],
                ]

    if targeting != (plan.audience_targeting or {}):
        plan_update["audience_targeting"] = targeting

    new_plan = plan.model_copy(update=plan_update) if plan_update else plan
    new_req = req.model_copy(update=req_update) if req_update else req
    return new_plan, new_req, applied, rejections


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


async def rebuild_summary(db, plan: CampaignPlan, req: CampaignRequest,
                          audience_text: str = "") -> Optional[dict]:
    """Re-derive Jane's reasoning block from the EDITED plan.

    Without this a save left the client with two descriptions of one campaign: the
    panel showing what they chose, and Jane's prose above still arguing for the budget,
    duration, pockets and interests she originally picked. Patching the numbers alone
    was not enough — the sentences name them too.

    The reach estimate is re-fetched rather than carried over, because changing the
    locations, interests, age, gender or placement is exactly what moves it. Reusing
    the old figure would quietly attach Jane's original audience size to the client's
    narrower one.

    Returns None if it cannot be rebuilt, and the caller keeps what it had — a stale
    summary is worse than a fresh one but far better than none.
    """
    from app.core.config import settings

    from .adapters.meta import MetaAdPlatformAdapter
    from .geo import meta_targeting_from_geo_named
    from .models import Platform
    from .summary import build_campaign_summary

    estimate = None
    if plan.platforms and plan.platforms[0].platform == Platform.META:
        try:
            adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
            # The SAME conversion + merge the real launch uses, so the estimate can
            # never promise a different audience from the one that actually ships.
            targeting = {
                **(await meta_targeting_from_geo_named(
                    plan.geo, region=(plan.geo.city if plan.geo else ""),
                    access_token=settings.META_ADS_ACCESS_TOKEN)),
                **plan.audience_targeting,
            }
            estimate = await adapter.get_delivery_estimate(targeting)
        except Exception as e:
            print(f"[PlanFields] reach estimate skipped on rebuild: {e}", flush=True)

    try:
        summary = build_campaign_summary(
            plan, req, delivery_estimate=estimate, audience_text=audience_text)
        return summary.model_dump(mode="json")
    except Exception as e:
        print(f"[PlanFields] summary rebuild failed: {e}", flush=True)
        return None
