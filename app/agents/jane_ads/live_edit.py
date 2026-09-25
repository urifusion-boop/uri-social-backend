"""
Jane + Ads — editing the targeting of a campaign that is already live.

Scope, deliberately narrow: this implements ONE row of the Campaign Management PRD's
action matrix (§19), "Audience or geography edit — approved supported existing settings
after provider validation". Interests, age, gender, placement and locations on an ad set
that already exists on Meta. Nothing else from that document: no monitoring loop, no
diagnosis catalogue, no complaints, no recommendations, no automation.

Budget, schedule, objective and bid strategy are NOT editable here. The PRD keeps them
in separate action families for good reason — they behave differently once spend has
happened, and Meta will not change an ad set's objective at all.

Four rules carried over from §20 and the CM acceptance criteria, each of which exists
because the obvious implementation is wrong:

· **Read the provider's state immediately before writing** (D45, CM17). A client may
  have edited the ad set in Ads Manager since the screen was drawn. Writing a payload
  built on our stale copy would silently revert their work. So an edit carries the
  baseline it was built from, and a changed baseline is a 409, not an overwrite.

· **An acknowledgement is not an effect** (CM13). Meta answering 200 means the request
  was accepted. The ad set is re-read afterwards and the result reported from what Meta
  actually holds, never from the fact that the call returned.

· **A timeout is unknown, not failed** (CM15). The write may well have applied. We
  re-read rather than retry, because retrying a mutation whose outcome we do not know
  is how duplicates happen.

· **Validate before mutating.** Meta's validate_only genuinely checks targeting —
  live-verified that age_min 5 and an invented interest id are both rejected — so an
  invalid edit is caught before anything changes.

One thing this cannot do anything about, and must therefore say plainly: changing the
targeting of a delivering ad set restarts Meta's learning. On a three-to-five day
campaign that is a large share of the run, so the caller is told, and told honestly that
we cannot predict whether it helps.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

# Only these move. Everything else on a live ad set is another action family.
EDITABLE = ("interests", "gender", "age_min", "age_max", "placement", "locations")


def targeting_fingerprint(targeting: dict) -> str:
    """A stable hash of the ad set's targeting, used as the edit's baseline.

    Compared immediately before a write: if Meta's current targeting no longer hashes
    to what the client was shown, somebody changed it in between and their change is
    not ours to discard.
    """
    return hashlib.sha256(
        json.dumps(targeting or {}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


# Meta files named places under one key per type, and each entry carries its own name.
_GEO_NAME_FIELDS = ("neighborhoods", "subcities", "cities", "regions")

# Only the markets Jane runs in; an unknown code falls back to the code itself.
_COUNTRY_NAMES = {"NG": "Nigeria", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa"}


def live_location_names(targeting: dict) -> list[str]:
    """The place names an ad set currently targets.

    Meta returns the names inline on geo_locations, so this needs no extra lookup —
    and reading them matters: showing "—" beside Locations for a campaign that really
    does target Ikeja G.R.A tells the client their ad is running nowhere.
    """
    names: list[str] = []
    geo = (targeting or {}).get("geo_locations") or {}
    for field in _GEO_NAME_FIELDS:
        for entry in geo.get(field) or []:
            name = (entry or {}).get("name")
            if name and name not in names:
                names.append(name)
    if names:
        return names
    # Country targeting is a bare code list, not named entries. Reporting "—" for an
    # ad set running across a whole country is the most misleading thing this panel
    # could say: the client reads it as "nowhere" when it means "everywhere".
    countries = geo.get("countries") or []
    return [f"{_COUNTRY_NAMES.get(c, c)} (nationwide)" for c in countries]


def describe_live(targeting: dict) -> list[dict[str, Any]]:
    """The live ad set's targeting as the same editable lines the review panel uses,
    minus everything that is not editable after launch."""
    from .plan_fields import describe
    from .models import (ABTestScope, CampaignPlan, CampaignRequest, CreativeContext,
                         GeoMode, GeoPin, GeoPlan, Goal, Platform, PlatformPlan,
                         PurchaseBehaviour)

    # describe() reads a plan, so give it a minimal one carrying this targeting. Reusing
    # it keeps the live editor and the pre-launch panel from drifting into two different
    # vocabularies for the same five settings.
    stub = CampaignPlan(
        business_id="", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.META, budget_ngn=1, days=1,
                                variants=1, test_scope=ABTestScope.NONE)],
        per_business_cap_ngn=0, account_cap_ngn=0, audience_targeting=targeting or {},
        geo=GeoPlan(mode=GeoMode.OWN_RADIUS,
                    pins=[GeoPin(name=n) for n in live_location_names(targeting)]),
    )
    req = CampaignRequest(business_id="", budget_ngn=1, creative=CreativeContext())
    return [f for f in describe(stub, req) if f["key"] in EDITABLE]


async def build_targeting_edit(
    current: dict, edits: dict[str, Any], region: str, access_token: str = "",
) -> tuple[Optional[dict], list[str], list[str]]:
    """Validate the requested changes and return the FULL targeting to send.

    Returns (targeting, applied, rejections). targeting is None when nothing survived
    validation, so the caller writes nothing rather than sending an unchanged payload
    and reporting a success that did not happen.

    Meta replaces the whole targeting object on write, so this starts from the ad set's
    current targeting and layers the edits on. Sending only the changed keys would drop
    every setting not mentioned — including the geo, which is not in this editable set.
    """
    from .models import (ABTestScope, CampaignPlan, CampaignRequest, CreativeContext,
                         GeoMode, GeoPlan, Goal, Platform, PlatformPlan, PurchaseBehaviour)
    from .plan_fields import apply_edits

    unsupported = [k for k in edits if k not in EDITABLE]
    rejections = [
        f"{k} cannot be changed on a campaign that has already launched." for k in unsupported
    ]
    wanted = {k: v for k, v in edits.items() if k in EDITABLE}
    if not wanted:
        return None, [], rejections

    stub = CampaignPlan(
        business_id="", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=Platform.META, budget_ngn=1, days=1,
                                variants=1, test_scope=ABTestScope.NONE)],
        per_business_cap_ngn=0, account_cap_ngn=0,
        geo=GeoPlan(mode=GeoMode.OWN_RADIUS, city=region),
        audience_targeting=dict(current or {}),
    )
    req = CampaignRequest(business_id="", budget_ngn=1, creative=CreativeContext(), geo=region)

    new_plan, _, applied, bad = await apply_edits(stub, req, wanted, access_token)
    rejections += bad
    if not applied:
        return None, [], rejections

    targeting = {**(current or {}), **new_plan.audience_targeting}
    # apply_edits DELETES these to mean "broad"; a merge would resurrect the old value.
    for key in ("genders", "publisher_platforms", "flexible_spec"):
        if key not in new_plan.audience_targeting:
            targeting.pop(key, None)

    if "locations" in applied and new_plan.geo and new_plan.geo.pins:
        from .geo import meta_targeting_from_geo_named
        geo_part = await meta_targeting_from_geo_named(
            new_plan.geo, region=region, access_token=access_token)
        targeting.update(geo_part)

    return targeting, applied, rejections


LEARNING_WARNING = (
    "Changing who an ad targets restarts Meta's learning for it — delivery can be "
    "uneven and more expensive for a day or so while it settles. On a short campaign "
    "that is a real share of the run, and there is no way to tell in advance whether "
    "the change will earn that back."
)


# ── TikTok: the same feature, built natively rather than forced through Meta's ──
# ── audience_targeting shape ─────────────────────────────────────────────────
#
# Added 2026-09-25, once adapters/tiktok.py's launch path had real gender/age/
# location targeting to edit (before that, TikTok ad groups only ever targeted
# "all adults in Nigeria" regardless of plan.audience_targeting, so there was
# nothing genuine here to build on — see plan_fields.py's own history for that
# earlier state).
#
# Kept deliberately separate from describe_live/build_targeting_edit above
# rather than forcing everything through one bidirectional Meta<->TikTok shape
# translation: Meta's targeting is a nested audience_targeting object matching
# Graph API's own shape (geo_locations, genders, age_min/age_max,
# flexible_spec); TikTok's is flat wire-format fields (location_ids, gender
# enum, age_groups bucket list) with no interests/placement equivalent at all.
# Two small, honest functions in TikTok's own vocabulary beat one that quietly
# only half-fits either platform.
#
# Same EDITABLE scope as Meta minus interests/placement (no TikTok mapping for
# interests yet, no TikTok concept of placement at all — TikTok only ever runs
# on TikTok's own placement).
TIKTOK_EDITABLE = ("locations", "gender", "age_min", "age_max")


async def describe_live_tiktok(
    targeting: dict, tiktok_advertiser_id: str, tiktok_access_token: str,
) -> list[dict[str, Any]]:
    """TikTok's own version of describe_live — the live ad group's current
    targeting (TikTok's native wire format) as the same editable-line shape
    describe_live gives Meta, minus interests/placement (see TIKTOK_EDITABLE)."""
    from .adapters.tiktok import _tiktok_age_range_from_groups, _tiktok_gender_choice, _tiktok_location_names
    from .plan_fields import MAX_AGE, MAX_LOCATIONS, MIN_AGE

    names = await _tiktok_location_names(
        targeting.get("location_ids") or [], tiktok_advertiser_id, tiktok_access_token)
    age_min, age_max = _tiktok_age_range_from_groups(targeting.get("age_groups") or [])
    return [
        {
            "key": "locations", "label": "Locations", "type": "list",
            "value": names, "editable": True, "max_items": MAX_LOCATIONS,
            "help": f"Up to {MAX_LOCATIONS} areas. Each must be somewhere TikTok can name — "
                    "we never target raw coordinates.",
        },
        {
            "key": "gender", "label": "Gender", "type": "select",
            "value": _tiktok_gender_choice(targeting.get("gender", "")),
            "options": ["all", "men", "women"], "editable": True,
            "help": "Who sees it. 'All' is usually right unless the product is gendered.",
        },
        {
            "key": "age_min", "label": "Minimum age", "type": "number",
            "value": age_min, "min": MIN_AGE, "max": MAX_AGE, "editable": True,
            "help": "TikTok targets in age bands, not this exact number — the range you "
                    "give gets mapped onto whichever of its bands overlap it.",
        },
        {
            "key": "age_max", "label": "Maximum age", "type": "number",
            "value": age_max, "min": MIN_AGE, "max": MAX_AGE, "editable": True,
        },
    ]


async def build_tiktok_targeting_edit(
    current: dict, edits: dict[str, Any], tiktok_advertiser_id: str, tiktok_access_token: str,
) -> tuple[Optional[dict], list[str], list[str]]:
    """TikTok's own version of build_targeting_edit.

    Returns (changed_fields, applied, rejections). Unlike Meta — which replaces
    the whole targeting object on write, so build_targeting_edit above returns
    the FULL merged targeting — TikTok's /adgroup/update/ is a partial update
    (same pattern TikTokAdsAdapter.set_delivery already relies on, sending only
    operation_status and nothing else), so this returns ONLY the keys that
    actually changed, in TikTok's own wire format, ready to hand straight to
    update_adgroup_targeting.
    """
    from .adapters.tiktok import _resolve_tiktok_locations, _tiktok_age_groups_for, _tiktok_age_range_from_groups
    from .plan_fields import MAX_AGE, MAX_LOCATIONS, MIN_AGE, _as_int

    unsupported = [k for k in edits if k not in TIKTOK_EDITABLE]
    rejections = [
        f"{k} cannot be changed on a campaign that has already launched." for k in unsupported
    ]
    wanted = {k: v for k, v in edits.items() if k in TIKTOK_EDITABLE}
    if not wanted:
        return None, [], rejections

    changed: dict[str, Any] = {}
    applied: list[str] = []

    if "locations" in wanted:
        names = [str(n) for n in (wanted.get("locations") or [])][:MAX_LOCATIONS]
        hits, bad = await _resolve_tiktok_locations(names, tiktok_advertiser_id, tiktok_access_token)
        rejections += [f"{n} — TikTok has no targetable area by that name in Nigeria" for n in bad]
        if hits:
            changed["location_ids"] = [h["location_id"] for h in hits]
            applied.append("locations")
        elif names:
            rejections.append("No location was changed — none of those could be named by TikTok.")

    if "gender" in wanted:
        choice = str(wanted.get("gender") or "").strip().lower()
        gender_map = {"all": "GENDER_UNLIMITED", "men": "GENDER_MALE", "women": "GENDER_FEMALE"}
        if choice not in gender_map:
            rejections.append(f"Gender must be one of: {', '.join(gender_map)}.")
        else:
            changed["gender"] = gender_map[choice]
            applied.append("gender")

    if {"age_min", "age_max"} & wanted.keys():
        current_lo, current_hi = _tiktok_age_range_from_groups(current.get("age_groups") or [])
        lo = _as_int(wanted.get("age_min", current_lo))
        hi = _as_int(wanted.get("age_max", current_hi))
        if lo is None or hi is None:
            rejections.append("Ages must be whole numbers.")
        elif not (MIN_AGE <= lo < hi <= MAX_AGE):
            rejections.append(
                f"Age range must sit between {MIN_AGE} and {MAX_AGE}, with the minimum below the maximum."
            )
        else:
            changed["age_groups"] = _tiktok_age_groups_for({"age_min": lo, "age_max": hi})
            applied += [k for k in ("age_min", "age_max") if k in wanted]

    if not changed:
        return None, applied, rejections
    return changed, applied, rejections
