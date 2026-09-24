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
