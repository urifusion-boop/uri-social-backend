"""Growth Intelligence: detection, diagnosis, policy, execution, evaluation and the
engine end to end over a fake Meta."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.agents.jane_ads.growth import actions as act
from app.agents.jane_ads.growth import creative, detect, diagnose, engine, evaluate, forecast, metrics
from app.agents.jane_ads.growth import policy as pol

mongomock_motor = pytest.importorskip("mongomock_motor")

TODAY = datetime.now(timezone.utc).date()
NOW = datetime.now(timezone.utc)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def day(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def rows(spec: dict, ad="ad1") -> list[dict]:
    """spec: {days_ago: (spend, impressions, clicks, conversations, reach)}"""
    out = []
    for n, (spend, imp, clicks, conv, *reach) in spec.items():
        out.append({"date": day(n), "ad_id": ad, "spend_ngn": spend, "impressions": imp,
                    "clicks": clicks, "conversations": conv, "reach": (reach[0] if reach else imp)})
    return out


def ctx(**over):
    base = {"today": TODAY, "status": "ACTIVE", "retrieved_at": NOW, "fetch_error": "",
            "daily_budget_ngn": 2000}
    base.update(over)
    return base


def issues(found):
    return {f["issue_type"] for f in found}


# ── detection ────────────────────────────────────────────────────────────────

def test_stale_data_blocks_everything_else():
    found = detect.detect(rows({1: (5000, 9000, 50, 0)}), ctx(retrieved_at=NOW - timedelta(hours=8)))
    assert issues(found) == {"tracking_or_api"} and found[0]["blocks_actions"]


def test_spend_without_results_needs_real_delivery():
    big = rows({1: (2000, 3000, 40, 0), 2: (2000, 3000, 40, 0), 3: (2000, 3000, 40, 0)})
    assert "spend_no_results" in issues(detect.detect(big, ctx()))
    tiny = rows({1: (200, 300, 2, 0)})
    assert "spend_no_results" not in issues(detect.detect(tiny, ctx()))


def test_cost_rise_compares_against_the_campaigns_own_past():
    base = {n: (1000, 2000, 40, 4) for n in range(4, 11)}          # ₦250 per conversation
    recent = {n: (1500, 2000, 40, 2) for n in range(1, 4)}         # ₦750
    found = detect.detect(rows({**base, **recent}), ctx())
    rise = [f for f in found if f["issue_type"] == "cost_rise"][0]
    assert rise["evidence"]["change"] > 1


def test_ctr_decline_flags_fatigue_from_frequency():
    base = {n: (1000, 4000, 80, 4, 3000) for n in range(4, 11)}
    recent = {n: (1000, 4000, 30, 4, 1000) for n in range(1, 4)}   # frequency 4
    found = [f for f in detect.detect(rows({**base, **recent}), ctx()) if f["issue_type"] == "ctr_decline"]
    assert found and found[0]["evidence"]["fatigue_likely"]


def test_a_stronger_ad_needs_a_significant_gap():
    good = rows({n: (500, 2000, 60, 10) for n in range(1, 8)}, ad="good")
    weak = rows({n: (500, 2000, 60, 2) for n in range(1, 8)}, ad="weak")
    found = [f for f in detect.detect(good + weak, ctx()) if f["issue_type"] == "strong_variant"]
    assert found and found[0]["evidence"]["weakest_ad"] == "weak"
    close = rows({n: (500, 2000, 60, 9) for n in range(1, 8)}, ad="close")
    assert not [f for f in detect.detect(good + close, ctx()) if f["issue_type"] == "strong_variant"]


def test_scale_opportunity_when_cheap_and_budget_bound():
    found = detect.detect(rows({n: (2000, 5000, 100, 8) for n in range(1, 4)}),
                          ctx(target_cpl_ngn=600))
    assert "scale_opportunity" in issues(found)


def test_follow_up_and_tracking_mismatch_from_the_inbox():
    found = detect.detect(rows({1: (300, 500, 10, 0)}),
                          ctx(leads={"recent_from_ads": 4}, followups={"waiting": 3, "oldest_hours": 5}))
    assert {"tracking_mismatch", "follow_up_gap"} <= issues(found)


# ── diagnosis ────────────────────────────────────────────────────────────────

def test_every_cause_is_a_labelled_hypothesis():
    good = rows({n: (500, 2000, 60, 10) for n in range(1, 8)}, ad="good")
    weak = rows({n: (500, 2000, 60, 2) for n in range(1, 8)}, ad="weak")
    f = [f for f in detect.detect(good + weak, ctx()) if f["issue_type"] == "strong_variant"][0]
    d = diagnose.diagnose(f, {"policy": pol.normalize({})})
    assert all(h["is_hypothesis"] for h in d["hypotheses"])
    rec = d["recommendations"][0]
    assert rec["action_type"] == "pause_ad" and rec["parameters"]["ad_id"] == "weak"
    assert rec["reversible"] and rec["success_metric"] and rec["eval_window_days"]


def test_scale_raise_respects_the_policy_increase():
    f = {"issue_type": "scale_opportunity", "evidence": {"daily_budget_ngn": 2000, "benchmark_cost_ngn": 600,
                                                         "recent": {"cost_per_conversation_ngn": 250}}}
    d = diagnose.diagnose(f, {"policy": pol.normalize({"max_increase_pct_7d": 10})})
    assert d["recommendations"][0]["parameters"] == {"from_ngn": 2000, "to_ngn": 2200}


# ── policy ───────────────────────────────────────────────────────────────────

STATE = {"data_fresh": True, "active": True, "launch_daily_ngn": 2000, "wallet_ngn": float("inf"), "markup": 1.1}
PAUSE = {"action_type": "pause_ad", "executable": True, "parameters": {"ad_id": "a"}}


def test_autonomous_needs_recorded_consent():
    assert pol.normalize({"mode": "autonomous"})["mode"] == "advisory"
    assert pol.normalize({"mode": "autonomous", "autonomous_authorized_by": "u1"})["mode"] == "autonomous"


def test_nothing_runs_alone_outside_autonomous_or_under_emergency_stop():
    adv = pol.normalize({})
    assert pol.check(adv, PAUSE, STATE, by_user=False) == (False, "Needs approval.")
    auto = pol.normalize({"mode": "autonomous", "autonomous_authorized_by": "u1"})
    assert pol.check(auto, PAUSE, STATE, by_user=False)[0]
    stopped = pol.normalize({**auto, "emergency_stop": True})
    assert not pol.check(stopped, PAUSE, STATE, by_user=False)[0]
    assert pol.check(stopped, PAUSE, STATE, by_user=True)[0]


def test_stale_data_cooldown_and_incidents_block_even_approved_changes():
    p = pol.normalize({})
    assert not pol.check(p, PAUSE, {**STATE, "data_fresh": False}, by_user=True)[0]
    assert not pol.check(p, PAUSE, {**STATE, "blocked_by": "Meta and inbox disagree"}, by_user=True)[0]
    assert not pol.check(p, PAUSE, {**STATE, "last_action_at": NOW - timedelta(hours=2)}, by_user=True)[0]


def test_budget_caps():
    raise_rec = {"action_type": "set_daily_budget", "executable": True,
                 "parameters": {"from_ngn": 2000, "to_ngn": 2400}}
    auto = pol.normalize({"mode": "autonomous", "autonomous_authorized_by": "u1",
                          "allowed_actions": ["set_daily_budget"]})
    assert not pol.check(auto, raise_rec, STATE, by_user=False)[0]          # above launch budget
    assert pol.check(pol.normalize({}), raise_rec, STATE, by_user=True)[0]   # a person may
    capped = pol.normalize({"max_daily_budget_ngn": 2200})
    assert not pol.check(capped, raise_rec, STATE, by_user=True)[0]         # an explicit cap binds
    assert not pol.check(pol.normalize({}), raise_rec, {**STATE, "budget_raised_7d_pct": 15}, by_user=True)[0]


def test_advisory_actions_never_execute():
    rec = {"action_type": "refresh_creative", "executable": False, "parameters": {}}
    assert not pol.check(pol.normalize({}), rec, STATE, by_user=True)[0]


# ── evaluation, creative tests, forecast ─────────────────────────────────────

def test_evaluation_is_inconclusive_on_small_samples():
    from app.agents.jane_ads.growth.stats import derive
    small = derive({"spend_ngn": 1000, "impressions": 1000, "reach": 800, "clicks": 20, "conversations": 2})
    assert evaluate.compare(small, small, "pause_ad")["verdict"] == "inconclusive"
    before = derive({"spend_ngn": 6000, "impressions": 9000, "reach": 7000, "clicks": 200, "conversations": 12})
    after = derive({"spend_ngn": 6000, "impressions": 9000, "reach": 7000, "clicks": 200, "conversations": 20})
    out = evaluate.compare(before, after, "pause_ad")
    assert out["verdict"] == "improved" and "not proof" in out["note"]


def test_creative_test_wont_crown_a_tiny_winner():
    variants = [{"ad_id": "a", "version": 1}, {"ad_id": "b", "version": 2}]
    tiny = rows({1: (100, 300, 5, 2)}, "a") + rows({1: (100, 300, 5, 0)}, "b")
    assert creative.compare(variants, tiny)["winner"] is None
    big = rows({n: (500, 3000, 90, 12) for n in range(1, 6)}, "a") + \
        rows({n: (500, 3000, 90, 3) for n in range(1, 6)}, "b")
    assert creative.compare(variants, big)["winner"] == "a"


def test_forecast_is_a_range_with_assumptions():
    r = rows({n: (1000, 2000, 40, 3 + n % 3) for n in range(1, 8)})
    f = forecast.project(r, NOW + timedelta(days=5))
    assert f["available"] and f["conversations_range"][0] <= f["conversations_range"][1] and f["assumptions"]
    assert not forecast.project(r[:2], NOW + timedelta(days=5))["available"]


# ── execution and the engine, end to end ─────────────────────────────────────

class FakeMeta:
    def __init__(self):
        self.ads = {"ad1": "ACTIVE", "weak": "ACTIVE"}
        self.campaign = "ACTIVE"
        self.daily = 2000.0
        self.calls = []

    async def fetch_adset_schedule(self, cid):
        return {"adset_id": "as1", "status": self.campaign, "effective_status": self.campaign,
                "daily_ngn": self.daily, "end_time": (NOW + timedelta(days=5)).isoformat()}

    async def set_delivery(self, cid, active):
        self.campaign = "ACTIVE" if active else "PAUSED"
        self.calls.append(("delivery", active))
        return {"status": self.campaign}


@pytest.fixture
def fake(monkeypatch):
    meta = FakeMeta()

    async def _read(adapter, node, fields):
        if node in meta.ads:
            return {"id": node, "status": meta.ads[node], "effective_status": meta.ads[node]}
        return {"id": node, "status": meta.campaign, "effective_status": meta.campaign}

    async def _post(adapter, node, body):
        meta.calls.append((node, body))
        if "status" in body:
            meta.ads[node] = body["status"]
        if "daily_budget" in body:
            meta.daily = body["daily_budget"] / 100
        return {"success": True}

    monkeypatch.setattr(act, "_read", _read)
    monkeypatch.setattr(act, "_post", _post)
    return meta


def test_execution_is_idempotent_and_verified(fake):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = {"campaign_id": "c1", "adset_id": "as1", "ad_id": "ad1", "brand_id": "b1"}
    rec = {"id": "r1", "action_type": "pause_ad", "parameters": {"ad_id": "weak"}, "summary": "Pause weak"}
    first = run(act.execute(db, fake, record, rec, actor="u1", mode="approval"))
    again = run(act.execute(db, fake, record, rec, actor="u1", mode="approval"))
    assert first["status"] == "executed" and first["verified"]
    assert first["before_state"]["status"] == "ACTIVE" and first["after_state"]["status"] == "PAUSED"
    assert again["action_id"] == first["action_id"]
    assert [c for c in fake.calls if c[0] == "weak"] == [("weak", {"status": "PAUSED"})]


def test_budget_change_reads_back_what_meta_holds(fake):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = {"campaign_id": "c1", "adset_id": "as1", "ad_id": "ad1", "brand_id": "b1"}
    rec = {"id": "r2", "action_type": "set_daily_budget", "parameters": {"from_ngn": 2000, "to_ngn": 2200}}
    out = run(act.execute(db, fake, record, rec, actor="u1", mode="approval"))
    assert out["verified"] and out["after_state"]["daily_budget_ngn"] == 2200
    assert ("as1", {"daily_budget": 220000}) in fake.calls


def _seed_engine(db, fake, monkeypatch, mode="advisory"):
    good = rows({n: (500, 2000, 60, 10) for n in range(1, 8)}, ad="ad1")
    weak = rows({n: (500, 2000, 60, 2) for n in range(1, 8)}, ad="weak")

    async def pull(db_, adapter, record, today=None):
        camp: dict = {}
        for r in good + weak:
            c = camp.setdefault(r["date"], {**r, "ad_id": "", "level": "campaign", "spend_ngn": 0,
                                            "impressions": 0, "reach": 0, "clicks": 0, "conversations": 0})
            for k in ("spend_ngn", "impressions", "reach", "clicks", "conversations"):
                c[k] += r[k]
        for c in camp.values():
            await db_[metrics.SNAPSHOTS].update_one(
                {"campaign_id": record["campaign_id"], "level": "campaign", "date": c["date"]},
                {"$set": {**c, "campaign_id": record["campaign_id"], "retrieved_at": NOW}}, upsert=True)
        for r in good + weak:
            await db_[metrics.SNAPSHOTS].update_one(
                {"campaign_id": record["campaign_id"], "level": "ad", "ad_id": r["ad_id"], "date": r["date"]},
                {"$set": {**r, "level": "ad", "campaign_id": record["campaign_id"], "retrieved_at": NOW}},
                upsert=True)
        await db_[metrics.HEALTH].update_one({"campaign_id": record["campaign_id"]},
                                             {"$set": {"last_success_at": NOW, "last_error": "",
                                                       "landing": {"checked": False, "checked_at": NOW.isoformat()}}},
                                             upsert=True)
        return {"ok": True, "rows": len(good + weak), "error": ""}

    monkeypatch.setattr(metrics, "pull", pull)
    record = {"campaign_id": "c1", "adset_id": "as1", "ad_id": "ad1", "brand_id": "b1",
              "user_id": "u1", "display_name": "Glow", "budget_ngn": 20000}
    run(db[engine.CAMPAIGNS].insert_one(dict(record)))
    if mode == "autonomous":
        run(db[engine.POLICIES].insert_one({"brand_id": "b1", "mode": "autonomous",
                                            "autonomous_authorized_by": "u1", "allowed_actions": ["pause_ad"],
                                            "cooldown_hours": 24}))
    return record


def test_engine_advisory_recommends_and_changes_nothing(fake, monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch)
    sent = []

    async def notify(rec, d, a=None):
        sent.append((d or {}).get("issue_type") or "action")

    out = run(engine.run_campaign(db, fake, record, notify))
    assert out["executed"] == 0 and out["new_recommendations"] >= 1
    rec = run(db[engine.RECOMMENDATIONS].find_one({"action_type": "pause_ad"}))
    assert rec["status"] == "proposed" and rec["approval_required"]
    assert fake.ads["weak"] == "ACTIVE"
    # A second pass updates the same diagnosis instead of stacking another.
    run(engine.run_campaign(db, fake, record, notify))
    assert run(db[engine.DIAGNOSES].count_documents({"issue_type": "strong_variant"})) == 1
    kinds = [e["type"] for e in run(db[engine.EVENTS].find({}).to_list(100))]
    assert {"metric_snapshot_received", "anomaly_detected", "diagnosis_created", "recommendation_created"} <= set(kinds)


def test_approval_runs_the_change_and_logs_it(fake, monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch)
    run(engine.run_campaign(db, fake, record))
    rec = run(db[engine.RECOMMENDATIONS].find_one({"action_type": "pause_ad"}, {"_id": 0}))
    entry = run(engine.approve(db, fake, record, rec, "u1"))
    assert entry["status"] == "executed" and fake.ads["weak"] == "PAUSED"
    assert run(db[engine.RECOMMENDATIONS].find_one({"id": rec["id"]}))["status"] == "executed"
    assert run(db[act.ACTIONS].find_one({"recommendation_id": rec["id"]}))["actor"] == "u1"


def test_autonomous_runs_only_allowed_actions_and_announces_them(fake, monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch, mode="autonomous")
    sent = []

    async def notify(rec, d, a=None):
        sent.append("action" if a else d["issue_type"])

    out = run(engine.run_campaign(db, fake, record, notify))
    assert out["executed"] == 1 and fake.ads["weak"] == "PAUSED" and "action" in sent
    entry = run(db[act.ACTIONS].find_one({}))
    assert entry["actor"] == "uri" and entry["mode"] == "autonomous"


def test_emergency_stop_halts_autonomy(fake, monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch, mode="autonomous")
    run(db[engine.POLICIES].update_one({"brand_id": "b1"}, {"$set": {"emergency_stop": True}}))
    out = run(engine.run_campaign(db, fake, record))
    assert out["executed"] == 0 and fake.ads["weak"] == "ACTIVE"


def test_landing_check_ignores_chat_destinations():
    assert run(metrics.check_landing_page("https://wa.me/2348000000000")) == {"checked": False}


def test_a_broken_page_matters_only_while_people_are_sent_there():
    broken = {"checked": True, "ok": False, "status": None, "error": "couldn't connect", "url": "https://x.test"}
    assert "landing_page" in issues(detect.detect([], ctx(landing=broken)))
    assert "landing_page" not in issues(detect.detect([], ctx(landing=broken, status="PAUSED")))
    d = diagnose.diagnose({"issue_type": "landing_page", "evidence": broken}, {})
    assert "couldn't connect" in d["hypotheses"][0]["why"]


def test_totals_come_from_campaign_rows_not_ad_rows():
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    run(db[metrics.SNAPSHOTS].insert_many([
        {"campaign_id": "c1", "level": "campaign", "ad_id": "", "date": day(1), "spend_ngn": 3800, "conversations": 14},
        {"campaign_id": "c1", "level": "ad", "ad_id": "a1", "date": day(1), "spend_ngn": 0, "conversations": 14},
        {"campaign_id": "c1", "ad_id": "old", "date": day(2), "spend_ngn": 5, "conversations": 0},
    ]))
    camp = run(metrics.load(db, "c1"))
    ads = run(metrics.load(db, "c1", level="ad"))
    assert [r["spend_ngn"] for r in camp] == [3800]
    assert {r["ad_id"] for r in ads} == {"a1", "old"}


def test_engine_reads_campaign_totals(fake, monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["growth"]
    record = _seed_engine(db, fake, monkeypatch)
    ctx = run(engine.context(db, fake, record, pol.normalize({})))
    assert sum(r["spend_ngn"] for r in ctx["rows"]) == 7000 and len(ctx["ad_rows"]) == 14


def test_incomplete_daily_figures_stand_trend_checks_down():
    zero_spend = rows({n: (0, 0, 0, 5) for n in range(1, 8)})
    found = detect.detect(zero_spend, ctx(daily_complete=False, lifetime_spend_ngn=3823.66, daily_spend_ngn=0,
                                          followups={"waiting": 1, "oldest_hours": 2}))
    assert issues(found) == {"daily_incomplete", "follow_up_gap"}
    assert [f for f in found if f["issue_type"] == "daily_incomplete"][0]["blocks_actions"]
    d = diagnose.diagnose([f for f in found if f["issue_type"] == "daily_incomplete"][0], {})
    assert "₦3,824" in d["hypotheses"][0]["why"] and not d["recommendations"][0]["executable"]


def test_lifetime_totals_parse_meta_shape():
    life = metrics.lifetime_totals([{"spend": "3823.66", "impressions": "4880", "clicks": "56", "reach": "4754",
                                     "actions": [{"action_type": "onsite_conversion.messaging_conversation_started_7d",
                                                  "value": "14"}]}])
    assert life == {"spend_ngn": 3823.66, "impressions": 4880, "reach": 4754, "clicks": 56, "conversations": 14}
