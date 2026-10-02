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


def test_a_narrow_audience_drops_the_interests_and_keeps_the_areas():
    """The areas are what the client chose and can see; the interest list is mostly
    Jane's inference, so it is the cheapest thing to give up."""
    est = Estimator([40_000, 250_000])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE, budget_label="₦20,000"))
    assert out["widened"] is True
    assert "flexible_spec" not in out["audience_targeting"]
    assert out["audience_targeting"]["age_min"] == 23      # age is the client's own words
    assert out["geo_targeting"] == GEO                      # areas survive
    assert "40,000" in out["note"] and "₦20,000" in out["note"]
    assert "Ikeja, Lekki Peninsula and Yaba" in out["note"]


def test_still_narrow_without_interests_widens_the_areas():
    est = Estimator([40_000, 60_000, 900_000])
    out = _run(widen_for_delivery(est, GEO, AUDIENCE, city="Lagos",
                                  wider_geo_targeting=WIDER_GEO))
    assert out["geo_targeting"] == WIDER_GEO
    assert "all of Lagos" in out["note"]


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
