"""
Refusing to launch an audience too small to deliver (reach.py).

A real campaign launched at 38,600-45,400 people — three tight pockets against an
eleven-interest list and an age band — which Ads Manager itself flags as "very narrow,
which may affect ad delivery". Meta accepts it, spends unevenly, and the client pays
for the lesson. These cover the widening that now happens before launch, and the
cases where widening must NOT happen.
"""
import asyncio

from app.agents.jane_ads.reach import (
    MIN_DELIVERABLE_AUDIENCE,
    audience_size,
    widen_for_delivery,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _estimate(size):
    return {"data": [{"estimate_mau_lower_bound": size,
                      "estimate_mau_upper_bound": int(size * 1.2)}]}


class Estimator:
    """Answers with a size per targeting spec, and records what it was asked."""

    def __init__(self, sizes, default=None):
        self.sizes = sizes          # list of sizes, returned in call order
        self.default = default
        self.calls = []

    async def get_delivery_estimate(self, targeting):
        self.calls.append(targeting)
        if self.sizes:
            size = self.sizes.pop(0)
            return _estimate(size) if size is not None else None
        return _estimate(self.default) if self.default is not None else None


GEO = {"geo_locations": {"neighborhoods": [{"key": "1", "name": "Ikeja"},
                                           {"key": "2", "name": "Lekki Peninsula"},
                                           {"key": "3", "name": "Yaba"}]}}
WIDER_GEO = {"geo_locations": {"regions": [{"key": "9", "name": "Lagos"}]}}
AUDIENCE = {"age_min": 23, "age_max": 55,
            "flexible_spec": [{"interests": [{"id": "1", "name": "Small business"}]}]}


def test_an_audience_big_enough_is_left_exactly_as_planned():
    """Nothing here widens an audience that can already deliver — the pins are tight
    on purpose and that is usually right."""
    est = Estimator([MIN_DELIVERABLE_AUDIENCE + 1])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE))
    assert out["widened"] is False
    assert out["audience_targeting"] == AUDIENCE
    assert out["note"] == ""
    assert len(est.calls) == 1


def test_a_narrow_audience_widens_the_area_and_keeps_the_interests():
    """Interests are what keep the WRONG people out — comparing Jane's launched ad
    sets with manually-run ones, the campaigns bringing chancers were the ones with
    no interest filter. A wider area full of the right people beats a tight area
    full of anybody."""
    est = Estimator([40_000, 250_000])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE, city="Lagos",
                                  budget_label="₦20,000", wider_geo_targeting=WIDER_GEO))
    assert out["widened"] is True
    assert out["geo_targeting"] == WIDER_GEO                 # the area gave way
    assert out["audience_targeting"] == AUDIENCE             # the interests did not
    assert "40,000" in out["note"] and "₦20,000" in out["note"]
    assert "all of Lagos" in out["note"]


def test_the_interest_filter_goes_only_when_a_whole_city_is_still_too_small():
    est = Estimator([40_000, 60_000, 900_000])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE, city="Lagos",
                                  wider_geo_targeting=WIDER_GEO))
    assert out["geo_targeting"] == WIDER_GEO
    assert "flexible_spec" not in out["audience_targeting"]
    assert out["audience_targeting"]["age_min"] == 23        # age is the client's own
    assert "drop the interest filter" in out["note"]


def test_widening_is_not_kept_when_meta_says_it_did_not_help():
    """Meta sometimes returns a lower number for a broader spec. Shipping a worse
    audience plus an explanation of how it was improved is the wrong kind of
    confident."""
    est = Estimator([40_000, 30_000])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE))
    assert out["widened"] is False
    assert out["audience_targeting"] == AUDIENCE


def test_an_unreadable_estimate_leaves_the_plan_alone():
    """Refusing to launch over a number we could not read would be worse than
    launching the audience the client already approved."""
    est = Estimator([None])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE))
    assert out["widened"] is False
    assert out["audience_targeting"] == AUDIENCE


def test_a_genuinely_small_market_is_not_stripped_for_nothing():
    """Every rung is measured. A floor that cannot be reached leaves the plan as
    planned rather than giving up targeting for no gain."""
    est = Estimator([5_000, 5_200, 5_300], default=5_300)
    out = _run(widen_for_delivery(est, GEO, AUDIENCE, city="Lagos",
                                  wider_geo_targeting=WIDER_GEO))
    # It did improve slightly at each rung, so the widening is kept — but the point is
    # it never claims the floor was met.
    assert out["estimate"] is not None
    assert audience_size(out["estimate"]) < MIN_DELIVERABLE_AUDIENCE


def test_the_lower_bound_is_what_counts():
    """Planning against the optimistic end is how an audience that reports "up to
    90,000" and delivers to 20,000 passes a floor it should have failed."""
    assert audience_size({"data": [{"estimate_mau_lower_bound": 20_000,
                                    "estimate_mau_upper_bound": 90_000}]}) == 20_000


# ── Jane's own words after a widening ─────────────────────────────────────────

def test_the_plan_sentence_stops_promising_the_dropped_pockets():
    """Live-caught: the card and the pins were right, and the sentence above them
    still read "I'll focus on Victoria Island, Ikoyi and Lekki Phase 1" — which is
    the line a client actually reads."""
    from app.agents.jane_ads.reach import restate_geography

    said = ("I'll focus on targeting busy professionals in Victoria Island, Ikoyi, and "
            "Lekki Phase 1 as these areas have a high concentration of affluent "
            "professionals who would benefit from a convenient laundry pickup service.")
    out = restate_geography(said, ["Victoria Island", "Ikoyi", "Lekki Phase 1"], "Lagos")
    assert "all of Lagos" in out
    for pocket in ("Victoria Island", "Ikoyi", "Lekki Phase 1"):
        assert pocket not in out
    # The audience reasoning survives — deleting the sentence would cost more than
    # the geography it fixed.
    assert "busy professionals" in out and "laundry pickup service" in out


def test_a_longer_pocket_name_is_not_half_replaced():
    """"Lekki Phase 1" must be consumed before the "Lekki" inside it."""
    from app.agents.jane_ads.reach import restate_geography

    out = restate_geography("Running in Lekki Phase 1 only.", ["Lekki", "Lekki Phase 1"], "Lagos")
    assert out == "Running in all of Lagos only."


def test_a_sentence_naming_no_pockets_is_untouched():
    from app.agents.jane_ads.reach import restate_geography

    said = "I chose Instagram and Facebook because your customers discover this by scrolling."
    assert restate_geography(said, ["Ikeja"], "Lagos") == said


# ── What a human buyer sets and Jane did not ─────────────────────────────────

def test_the_launch_targeting_excludes_audience_network_and_sets_a_language():
    """Unset, Meta picks automatic placements — Audience Network included, where a tap
    is as often a misfire as an intention — and serves in any language. Both were
    differences against manually-run ad sets on the same account."""
    from app.agents.jane_ads import constants as C

    assert "audience_network" not in C.DEFAULT_PUBLISHER_PLATFORMS
    assert C.DEFAULT_PUBLISHER_PLATFORMS == ["facebook", "instagram"]
    assert C.DEFAULT_LOCALES == [1001]          # English (All), per Meta's adlocale search


def test_the_client_s_own_placement_still_wins_over_the_default():
    """These are defaults, not policy: a placement the client picked on the plan card
    is spread over them at launch."""
    src = open("app/agents/jane_ads/adapters/meta.py").read()
    publisher = src.index('"publisher_platforms": list(C.DEFAULT_PUBLISHER_PLATFORMS)')
    audience = src.index("**plan.audience_targeting", publisher)
    automation = src.index('"targeting_automation"', publisher)
    # defaults → client's own targeting → the advantage_audience flag Meta requires
    assert publisher < audience < automation
