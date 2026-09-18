"""
Bucket queries and the sample-size rules (CI-SPEC-01 §2.4, acceptance 9/10/12).

§2.4 requires the thresholds be enforced at the QUERY LAYER, not by convention: "a
claim below threshold should be impossible to surface, not merely discouraged". These
tests exist because a ranking returned with a warning attached is still a ranking on
screen, and a chart is precisely what makes six campaigns look like evidence.
"""
from app.agents.jane_ads import buckets as B
from app.agents.jane_ads.campaign_record import is_exploration


def _rec(cost=None, fmt="SEED-001", brand="b1", budget=15_000.0, mods=None,
         diverged=False, exploration=False, created="2026-01-01"):
    return {
        "brand_id": brand,
        "created_at": created,
        "exploration": exploration,
        "context": {"business_category": "food_beverage", "city": "lagos",
                    "budget_tier": "standard", "platform": "meta"},
        "creative": {"format_id": fmt, "asset_source": "generate", "media_type": "static"},
        "budget": {"stated_ngn": budget},
        "modifications": mods or [],
        "strategy": {"recommendation_diverged": diverged, "geo_strategy": "watering_hole"},
        "results": None if cost is None else {"cost_per_conversation_ngn": cost},
    }


# ── Thresholds ───────────────────────────────────────────────────────────────

def test_threshold_states_match_the_spec():
    assert B.threshold_state(9) == "insufficient"
    assert B.threshold_state(10) == "observation_only"
    assert B.threshold_state(30) == "may_bias_defaults"
    assert B.threshold_state(50) == "claimable"


def test_a_thin_bucket_returns_no_ranking_at_all():
    """Acceptance 9. Not a ranking with a caveat — no rows whatsoever."""
    out = B.compare([_rec(cost=500) for _ in range(6)], "creative_format")
    assert out["rows"] == []
    assert out["threshold_state"] == "insufficient"
    assert "too few" in out["message"]


def test_a_sufficient_bucket_ranks_by_cost_per_conversation():
    records = ([_rec(cost=900, fmt="SEED-A") for _ in range(5)]
               + [_rec(cost=400, fmt="SEED-B") for _ in range(5)])
    out = B.compare(records, "creative_format")
    assert out["threshold_state"] == "observation_only"
    assert [r["value"] for r in out["rows"]] == ["SEED-B", "SEED-A"]
    assert out["rows"][0]["median_cost_per_conversation_ngn"] == 400


def test_every_comparison_carries_its_sample_size_and_what_it_licenses():
    """Acceptance 10 — a number must never appear without the count behind it."""
    out = B.compare([_rec(cost=500) for _ in range(12)], "creative_format")
    assert out["campaigns"] == 12
    assert out["threshold_state"] == "observation_only"
    assert out["thresholds"] == {"observe": 10, "bias": 30, "claim": 50}


def test_a_thin_group_inside_a_full_bucket_is_marked_insufficient():
    """The same error at a smaller scale: one campaign in a row is not a finding."""
    records = ([_rec(cost=600, fmt="SEED-A") for _ in range(11)]
               + [_rec(cost=100, fmt="SEED-RARE")])
    rows = {r["value"]: r for r in B.compare(records, "creative_format")["rows"]}
    assert rows["SEED-RARE"]["sufficient"] is False
    assert rows["SEED-A"]["sufficient"] is True


def test_records_without_results_do_not_fake_a_median():
    out = B.compare([_rec(cost=None) for _ in range(12)], "creative_format")
    assert out["rows"][0]["median_cost_per_conversation_ngn"] is None
    assert out["rows"][0]["with_results"] == 0


# ── Headline metrics ─────────────────────────────────────────────────────────

def test_repeat_rate_and_budget_raise_are_counted_per_brand():
    """The sleeper metrics: no integration, cannot be gamed. A client who ran ₦15k
    then came back at ₦30k has said something no survey would get."""
    records = [
        _rec(brand="b1", budget=15_000, created="2026-01-01"),
        _rec(brand="b1", budget=30_000, created="2026-02-01"),
        _rec(brand="b2", budget=10_000, created="2026-01-01"),
    ]
    m = B.headline_metrics(records)
    assert m["repeat_rate"] == 0.5         # one of two brands came back
    assert m["raised_budget_on_repeat"] == 1


def test_plan_acceptance_counts_campaigns_the_client_did_not_edit():
    records = [_rec(), _rec(), _rec(mods=[{"field": "budget"}])]
    assert B.headline_metrics(records)["plan_acceptance_rate"] == round(2 / 3, 3)


def test_divergence_rate_is_reported():
    records = [_rec(diverged=True), _rec(), _rec(), _rec()]
    assert B.headline_metrics(records)["recommendation_divergence_rate"] == 0.25


# ── Exploration reserve (§2.5, acceptance 12) ────────────────────────────────

def test_exploration_is_stable_for_a_given_campaign():
    """Derived from the id, not drawn at random: a record that flipped between runs
    would corrupt the comparison the reserve exists to protect."""
    assert is_exploration("52565013550010") == is_exploration("52565013550010")


def test_exploration_lands_near_the_target_share():
    ids = [str(52565000000000 + i) for i in range(2000)]
    share = sum(is_exploration(i) for i in ids) / len(ids)
    assert 0.08 <= share <= 0.22, share


def test_exploration_share_is_reported_so_a_shortfall_is_visible():
    records = [_rec(exploration=True)] + [_rec() for _ in range(9)]
    assert B.headline_metrics(records)["exploration_share"] == 0.1
