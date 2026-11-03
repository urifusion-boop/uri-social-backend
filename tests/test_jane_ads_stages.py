"""Explore → Validate → Scale: creative verdicts against the business's target, the
campaign's stage, the budget-aware test structure, and the engine wiring."""
from app.agents.jane_ads.growth import detect, diagnose, engine
from app.agents.jane_ads.growth import stages as st

from tests.test_jane_ads_growth import _seed_engine, fake, mongomock_motor, run  # noqa: F401


def test_no_result_is_inconclusive_until_the_spend_could_have_produced_one():
    early = st.judge(500, 0, 2000)
    assert early["status"] == st.INCONCLUSIVE and early["spend_to_judge_ngn"] == 4500
    assert st.judge(10000, 0, 1500)["status"] == st.UNDERPERFORMING


def test_on_target_is_promising_and_well_over_is_underperforming():
    assert st.judge(4000, 4, 1500)["status"] == st.PROMISING
    assert st.judge(1000, 1, 1500)["confidence"] == "low"
    assert st.judge(9000, 3, 1500)["status"] == st.UNDERPERFORMING
    near = st.judge(5000, 3, 1500)
    assert near["status"] == st.INCONCLUSIVE and "near" in near["reason"]


def test_strong_needs_enough_results_on_separate_days():
    assert st.judge(6000, 12, 1000, days_with_results=3)["status"] == st.STRONG
    assert st.judge(6000, 12, 1000, days_with_results=1)["status"] == st.PROMISING
    assert st.judge(6000, 6, 1000, days_with_results=5)["status"] == st.PROMISING


def test_without_a_target_nothing_is_called_a_loser():
    v = st.judge(50000, 0, None)
    assert v["status"] == st.INCONCLUSIVE and "No target" in v["reason"]


def test_orders_replace_conversations_once_outcomes_are_recorded():
    goal = {"target_cpl_ngn": 500, "target_cost_per_order_ngn": 2000}
    thin = st.yardstick(20, {"with_outcome": 2, "by_stage": {"converted": 1}}, goal)
    assert thin["metric"] == "conversations" and thin["target_ngn"] == 500
    rich = st.yardstick(20, {"with_outcome": 10, "by_stage": {"converted": 2}}, goal)
    assert rich == {"metric": "orders", "results": 2.0, "target_ngn": 2000.0}


def test_cheapest_enquiries_are_not_always_the_cheapest_sales():
    goal = {"target_cpl_ngn": 600, "target_cost_per_order_ngn": 1500}
    a = st.yardstick(20, {"with_outcome": 20, "by_stage": {"converted": 2}}, goal)
    c = st.yardstick(16, {"with_outcome": 16, "by_stage": {"converted": 8}}, goal)
    assert st.judge(10000, a["results"], a["target_ngn"], a["metric"])["status"] == st.UNDERPERFORMING
    assert st.judge(10000, c["results"], c["target_ngn"], c["metric"])["status"] == st.PROMISING


def _v(ad, status, results, cost=None, spend_to_judge=None, concept=None):
    return {"ad_id": ad, "status": status, "results": results, "cost_per_result_ngn": cost,
            "spend_to_judge_ngn": spend_to_judge, "attributes": {"concept": concept} if concept else {}}


def test_stage_moves_explore_to_validate_to_scale():
    assert st.stage([_v("a", st.STRONG, 12, 400)], True)["stage"] == st.SCALE
    assert st.stage([_v("a", st.PROMISING, 12, 400)], True)["stage"] == st.VALIDATE
    v = st.stage([_v("a", st.PROMISING, 4, 400, concept="appetite appeal"), _v("b", st.INCONCLUSIVE, 1)], True)
    assert v["stage"] == st.VALIDATE and "appetite appeal" in v["next"]
    e = st.stage([_v("a", st.INCONCLUSIVE, 0, spend_to_judge=3000), _v("b", st.INCONCLUSIVE, 0, spend_to_judge=1000)], True)
    assert e["stage"] == st.EXPLORE and "₦4,000" in e["next"]
    assert st.stage([_v("a", st.STRONG, 12, 400)], False)["stage"] == st.EXPLORE


def test_structure_fits_the_budget_and_says_when_it_cannot():
    small = st.recommend_structure(2000, 3, 1500)
    assert small["structure"] == "single" and small["concepts"] == 1
    tiny = st.recommend_structure(2000, 3, 5000)
    assert tiny["budget_too_small"] and tiny["days_to_judge"] == 7
    medium = st.recommend_structure(10000, 7, 1500)
    assert medium["structure"] == "abo" and medium["concepts"] == 5 and medium["per_concept_daily_ngn"] == 2000
    capped = st.recommend_structure(10000, 7, 1500, concepts_available=3)
    assert capped["concepts"] == 3 and capped["days_to_judge"] == 2
    assert st.recommend_structure(10000, 7, 1500, proven=2)["structure"] == "cbo"


def test_only_a_running_ad_beside_a_good_one_is_flagged():
    weak = {"ad_id": "w", "status": st.UNDERPERFORMING, "spend_ngn": 9000, "results": 1}
    good = {"ad_id": "g", "status": st.PROMISING, "spend_ngn": 3000, "results": 5}
    found = detect._underperformer_findings([weak, good], [])
    assert [f["entity_id"] for f in found] == ["w"]
    assert detect._underperformer_findings([weak], []) == []
    assert detect._underperformer_findings([weak, good], [{"ad_id": "w", "status": "PAUSED"}]) == []
    d = diagnose.diagnose(found[0], {})
    assert d["recommendations"][0]["action_type"] == "pause_ad"
    assert d["recommendations"][0]["parameters"] == {"ad_id": "w"}


def test_engine_judges_against_the_target_and_logs_experiments(fake, monkeypatch):  # noqa: F811
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch)
    run(db[engine.GOALS].insert_one({"campaign_id": "c1", "target_cpl_ngn": 100}))
    run(engine.run_campaign(db, fake, record))
    logged = {e["ad_id"]: e for e in run(db[engine.EXPERIMENTS].find({}).to_list(10))}
    assert logged["ad1"]["status"] == st.STRONG and logged["ad1"]["stage"] == st.SCALE
    assert logged["weak"]["status"] == st.UNDERPERFORMING
    d = run(db[engine.DIAGNOSES].find_one({"issue_type": "creative_underperforming"}))
    assert d["entity_id"] == "weak" and fake.ads["weak"] == "ACTIVE"
    run(engine.run_campaign(db, fake, record))
    assert run(db[engine.EXPERIMENTS].count_documents({})) == 2
