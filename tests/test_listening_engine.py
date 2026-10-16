"""
Listening Intelligence engine: queries compiled from data, topic resolution,
coverage-aware detection (rising / spike / emerging / recurring need, breadth
guard), card gating and number safety, collection coverage and budget. Uses an
in-memory MongoDB; no network, no AI calls (AI steps are stubbed).
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import collector, detect, markets, personalize, topics
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge
from app.agents.market_intelligence.listening import profile as profile_mod
from app.agents.market_intelligence.models import RawEvidence

NOW = datetime(2026, 10, 16, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET = {"key": "hair_wigs:NG", "category_id": "hair_wigs", "country": "NG", "label": "Hair & wigs · Nigeria",
          "created_at": NOW - timedelta(days=30), "learned_terms": []}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["listen_test"]


# ── queries ─────────────────────────────────────────────────────────────────

def test_queries_are_compiled_from_data_with_intent_first():
    cat = {"core_terms": ["wig", "frontal wig", "bone straight"], "local_terms": {"pcm": ["who dey sell wig"]},
           "excluded": ["big wig"]}
    patterns = {"en": {"purchase": ["who sells", "where can i buy"], "recommendation": ["recommend"],
                       "problem": ["fake"], "how_to": []},
                "pcm": {"purchase": ["who dey sell"], "recommendation": [], "problem": [], "how_to": []}}
    plan = markets.compile_queries(cat, "NG", patterns, ["latasha wig"], max_queries=10)
    qs = [q["q"] for q in plan["queries"]]
    assert qs[0] == '("who sells" OR "where can i buy" OR recommend) (wig OR "frontal wig" OR "bone straight")'
    assert any(q["kind"] == "intent:pcm" for q in plan["queries"])
    assert any('"who dey sell wig"' in q for q in qs)            # local-language core term
    assert any(q["kind"] == "learned" and '"latasha wig"' in q["q"] for q in plan["queries"])
    assert plan["excluded"] == ["big wig"] and plan["languages"] == ["en", "pcm"]
    assert all(len(q) <= markets.MAX_QUERY_CHARS + 120 for q in qs)


def test_query_count_is_capped():
    cat = {"core_terms": [f"term{i}" for i in range(80)], "excluded": []}
    plan = markets.compile_queries(cat, "GB", {"en": {"purchase": ["buy"]}}, [], max_queries=3)
    assert len(plan["queries"]) == 3 and plan["dropped"] >= 1


# ── topics ──────────────────────────────────────────────────────────────────

def test_normalize():
    assert topics.normalize("Latasha Wigs") == "latasha wig"
    assert topics.normalize("#LatashaWig") == "latashawig"
    assert topics.normalize("dress") == "dress"
    assert topics.normalize("hair") is None            # too generic to be a topic


def test_hashtag_and_plural_forms_resolve_to_one_topic():
    db = fresh_db()

    async def go():
        a = await topics.resolve(db, "hair_wigs:NG", "latasha wig", NOW)
        b = await topics.resolve(db, "hair_wigs:NG", "#LatashaWig", NOW)
        c = await topics.resolve(db, "hair_wigs:NG", "Latasha wigs", NOW)
        other_market = await topics.resolve(db, "beauty_skincare:NG", "latasha wig", NOW)
        doc = await db["mi_market_topics"].find_one({"market": "hair_wigs:NG", "key": "latasha wig"})
        return a, b, c, other_market, doc
    a, b, c, other, doc = run(go())
    assert a == b == c == "latasha wig" and other == "latasha wig"
    assert "latashawig" in doc["aliases"]


# ── detection ───────────────────────────────────────────────────────────────

async def _seed(db, per_day_authors: dict[int, int], topic="latasha wig", covered_days=12, intent="purchase",
                same_author=False):
    """per_day_authors: {days_ago: n distinct accounts}. Full coverage for covered_days."""
    await db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                           "covered_from": NOW - timedelta(days=covered_days), "until": NOW,
                                           "started_at": NOW})
    await topics.resolve(db, MARKET["key"], topic, NOW - timedelta(days=20))
    n = 0
    for ago, count in per_day_authors.items():
        for i in range(count):
            n += 1
            ts = NOW - timedelta(days=ago, hours=1 + (i % 20))
            sid = f"x:{ago}-{i}"
            author = "@one" if same_author else f"@a{ago}_{i}"
            item = {"source_id": sid, "author_handle": author, "published_at": ts, "raw_metrics": {"likes": i},
                    "text": f"who sells {topic} in lagos {i}", "url": f"https://x.com/s/{sid}"}
            reading = {"source_id": sid, "market": MARKET["key"], "on_topic": True, "intent": intent,
                       "need": "find a trusted vendor", "sentiment": "neutral", "buying_intent": "high",
                       "topics": [topic], "confidence": 0.9, "published_at": ts, "author": author}
            await db["mi_listen_items"].insert_one(dict(item))
            await db["mi_listen_readings"].insert_one(dict(reading))
            await topics.count(db, MARKET["key"], topic, item, reading)


def _detect(db):
    return run(detect.detect_market(db, MARKET, CFG, NOW))


def test_rising_needs_baseline_growth_and_persistence():
    db = fresh_db()
    run(_seed(db, {0: 12, 1: 11, **{d: 1 for d in range(2, 11)}}))
    sigs = [s for s in _detect(db) if s["topic"] == "latasha wig"]
    rising = [s for s in sigs if s["type"] == "rising"]
    assert rising, sigs
    s = rising[0]
    assert s["current"]["authors"] == 12 and s["baseline"]["median_per_day"] == 1.0
    assert s["persistence_days"] >= 2 and s["scores"]["C"] > 50
    assert 0 < s["confidence"] <= 1 and s["samples"]


def test_one_day_jump_is_a_spike_not_a_trend():
    db = fresh_db()
    run(_seed(db, {0: 12, **{d: 1 for d in range(1, 11)}}))
    types = {s["type"] for s in _detect(db) if s["topic"] == "latasha wig"}
    assert "spike" in types and "rising" not in types


def test_one_account_cannot_make_a_signal():
    db = fresh_db()
    run(_seed(db, {0: 12, 1: 11, **{d: 1 for d in range(2, 11)}}, same_author=True))
    assert not [s for s in _detect(db) if s["type"] in ("rising", "spike")]


def test_short_history_is_only_an_emerging_signal():
    db = fresh_db()
    run(_seed(db, {0: 9, 1: 2, 2: 1}, covered_days=3))
    types = {s["type"] for s in _detect(db) if s["topic"] == "latasha wig"}
    assert "emerging" in types and "rising" not in types


def test_uncovered_hours_are_not_counted():
    db = fresh_db()
    run(_seed(db, {0: 12, 1: 11, **{d: 1 for d in range(2, 11)}}))
    run(db["mi_listen_runs"].delete_many({}))          # nothing was actually checked
    assert _detect(db) == []


def test_steady_questions_become_a_recurring_need():
    db = fresh_db()
    run(_seed(db, {d: 1 for d in range(0, 10)}, topic="frontal install", intent="question"))
    rec = [s for s in _detect(db) if s["type"] == "recurring_need"]
    assert rec and rec[0]["current"]["authors"] == 10 and rec[0]["current"]["days_present"] == 10


# ── personalisation ─────────────────────────────────────────────────────────

def test_observation_numbers_come_from_the_signal():
    sig = {"type": "rising", "topic": "latasha wig", "topic_name": "latasha wig", "market_label": "Hair & wigs · Nigeria",
           "current": {"authors": 12}, "baseline": {"median_per_day": 1.0, "days": 9}}
    text = personalize.observation(sig)
    assert "12 different accounts" in text and "usual 1.0 a day" in text and "9 days" in text


def test_made_up_statistics_are_removed():
    out = personalize.strip_statistics("Demand is up 45% this week. It fits your lace wigs. 2,000 people want it.")
    assert out == "It fits your lace wigs."
    assert personalize.strip_statistics("Film 3 ways to style it.") == "Film 3 ways to style it."


def test_score_uses_configured_weights():
    sig = {"scores": {"M": 80, "C": 70}, "confidence": 0.6}
    s = personalize.score(sig, relevance=90, brand_fit=50, weights=CFG["score"]["weights"])
    assert s["E"] == 60 and s["total"] == round(0.3 * 90 + 0.25 * 80 + 0.2 * 70 + 0.15 * 60 + 0.1 * 50, 1)


class _J:
    def __init__(self, idx, rel, fit, why="Fits your catalogue. Up 300% today!"):
        self.idx, self.relevance, self.brand_fit, self.why = idx, rel, fit, why
        self.action = personalize._Action(type="video", title="Film 3 ways to style the Latasha wig — 50% off",
                                          platforms=["instagram", "tiktok"], format="Reel 30s", hook="Hook",
                                          outline=["a", "b", "c"], cta="DM to order", caption="Caption")


def test_cards_are_gated_scored_and_stored(monkeypatch):
    db = fresh_db()
    run(_seed(db, {0: 12, 1: 11, **{d: 1 for d in range(2, 11)}}))
    run(_seed(db, {0: 12, 1: 11, **{d: 1 for d in range(2, 11)}}, topic="bob wig"))
    _detect(db)
    for s in run(db["mi_listen_signals"].find({}).to_list(None)):
        run(db["mi_listen_signals"].update_one({"_id": s["_id"]}, {"$set": {"computed_at": NOW}}))

    async def fake_judge(profile, sigs, model):
        return {i: _J(i, 90 if s["topic"] == "latasha wig" else 20, 80) for i, s in enumerate(sigs)}
    monkeypatch.setattr(personalize, "_judge", fake_judge)
    prof = {"brand_id": "b1", "user_id": "u1", "markets": [MARKET["key"]], "facts": {"brand_name": "Glow Hair"}}
    cards = run(personalize.cards_for(db, prof, CFG, NOW))
    assert [c["topic"] for c in cards] == ["latasha wig"]           # irrelevant topic gated out
    c = cards[0]
    assert "300%" not in c["why"] and c["why"] == "Fits your catalogue."
    assert "%" not in c["action"]["title"]
    assert c["scores"]["total"] >= CFG["score"]["min_score"] and c["evidence"]["scope_note"]
    # a live card is not repeated; a dismissed topic stays quiet
    assert run(personalize.cards_for(db, prof, CFG, NOW)) == []
    run(db["mi_listen_cards"].update_one({"id": c["id"]}, {"$set": {"status": "dismissed", "updated_at": NOW}}))
    assert run(personalize.cards_for(db, prof, CFG, NOW)) == []


# ── collection ──────────────────────────────────────────────────────────────

class _Page:
    def __init__(self, evidence):
        self.evidence, self.has_more, self.next_cursor, self.gaps = evidence, False, None, []


class _FakeAdapter:
    def __init__(self, n, fail=False):
        self.n, self.fail = n, fail

    async def start_collection(self, kw, ex, since, until, items_per_query=None):
        if self.fail:
            raise RuntimeError("apify down")
        return "run1"

    async def fetch_page(self, run_id, cursor=None):
        return _Page([RawEvidence(provider="x", platform="x", source_id=f"x:{i}", text=f"who sells wig {i}",
                                  published_at=NOW - timedelta(hours=i), collected_at=NOW, author_handle=f"@u{i}")
                      for i in range(self.n)])

    async def actual_cost(self, run_id):
        return 0.01


def test_capped_search_is_covered_from_its_oldest_post():
    since = NOW - timedelta(days=7)
    capped = run(collector._one_query(_FakeAdapter(50), {"q": "(wig)", "kind": "core"}, [], since, NOW, 50))
    full = run(collector._one_query(_FakeAdapter(10), {"q": "(wig)", "kind": "core"}, [], since, NOW, 50))
    failed = run(collector._one_query(_FakeAdapter(0, fail=True), {"q": "(wig)", "kind": "core"}, [], since, NOW, 50))
    assert capped["capped"] and capped["covered_from"] == NOW - timedelta(hours=49)
    assert not full["capped"] and full["covered_from"] == since
    assert failed["covered_from"] is None and failed["error"]


def test_collection_stops_at_the_monthly_budget():
    db = fresh_db()
    run(db["mi_listen_runs"].insert_one({"market": "x", "started_at": NOW, "cost_usd": 99.0}))
    res = run(collector.collect_market(db, MARKET, CFG, _FakeAdapter(5), NOW))
    assert res["status"] == "skipped_budget"


def test_country_from_location_text():
    assert profile_mod._country_from_text("Lekki, Lagos") == "NG"
    assert profile_mod._country_from_text("Accra and Kumasi") == "GH"
    assert profile_mod._country_from_text("Africa") is None
