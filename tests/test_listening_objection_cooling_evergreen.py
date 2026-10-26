"""
Three gaps from the Listening module audit, all in detect.py/personalize.py
(PRD §8.3's signal table and §9.2's evergreen note):

1. objection_shift — a rise in complaint-intent volume for a topic, measured
   independently of the topic's overall mention volume (so a real spike in
   doubt/complaint isn't missed just because overall chatter stayed flat).
2. cooling — a topic that had a live rising/spike/new_entity signal, whose
   momentum has since fallen back toward/below its own baseline: the prior
   signal and any live card for it are expired right away ("stop
   recommending; expire cards"), not left to run out their normal TTL.
3. Evergreen protection — recurring_need signals/cards get a reserved slot
   so they aren't crowded out of judging/cards by trending signals, which
   always win a combined-score sort since recurring_need's momentum score
   is flat and low by design.
4. upcoming_development — enough independent people already mentioning a
   concrete FUTURE date for a launch, event, sale or regulation
   (enrich.py's event_date field, extracted by the same AI read as every
   other per-post field). Evidence-based like recurring_need, not
   baseline/z-score based.

Uses an in-memory MongoDB; no real AI calls (AIService/_judge are stubbed).
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import detect, personalize, topics
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 16, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET = {"key": "hair_wigs:NG", "category_id": "hair_wigs", "country": "NG", "label": "Hair & wigs · Nigeria",
          "created_at": NOW - timedelta(days=60), "learned_terms": []}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["listen_obj_cool_test"]


async def _post(db, topic, author, intent, ts, sentiment="neutral", event_date=None):
    sid = f"x:{intent}:{author}:{ts.isoformat()}"
    item = {"source_id": sid, "author_handle": author, "published_at": ts, "raw_metrics": {"likes": 1},
            "text": f"about {topic}", "url": f"https://x.com/s/{sid}"}
    reading = {"source_id": sid, "market": MARKET["key"], "on_topic": True, "measured": True, "intent": intent,
               "need": "fit concern" if intent == "complaint" else "find a vendor", "sentiment": sentiment,
               "buying_intent": "low", "topics": [topic], "confidence": 0.9, "published_at": ts, "author": author,
               "event_date": event_date}
    await db["mi_listen_items"].insert_one(dict(item))
    await db["mi_listen_readings"].insert_one(dict(reading))
    await topics.count(db, MARKET["key"], topic, item, reading)


def _detect(db, now=NOW):
    return run(detect.detect_market(db, MARKET, CFG, now))


# ── objection_shift ───────────────────────────────────────────────────────

async def _seed_objection(db, topic="latasha wig"):
    await db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                           "covered_from": NOW - timedelta(days=12), "until": NOW,
                                           "started_at": NOW})
    await topics.resolve(db, MARKET["key"], topic, NOW - timedelta(days=30))
    # baseline (days 1-10): steady 10 purchase authors/day + 1 complaint author/day
    for ago in range(1, 11):
        for i in range(10):
            await _post(db, topic, f"@buy{ago}_{i}", "purchase", NOW - timedelta(days=ago, hours=1 + i))
        await _post(db, topic, f"@complain{ago}", "complaint", NOW - timedelta(days=ago, hours=1), "negative")
    # today: same purchase volume (no overall trend) + a real complaint spike
    for i in range(10):
        await _post(db, topic, f"@buy0_{i}", "purchase", NOW - timedelta(hours=1 + i))
    for i in range(6):
        await _post(db, topic, f"@complain0_{i}", "complaint", NOW - timedelta(hours=1 + i), "negative")


def test_objection_shift_fires_on_complaint_spike_without_overall_trend():
    db = fresh_db()
    run(_seed_objection(db))
    sigs = [s for s in _detect(db) if s["topic"] == "latasha wig"]
    types = {s["type"] for s in sigs}
    assert "objection_shift" in types
    assert "rising" not in types and "spike" not in types  # overall volume never trended

    obj = next(s for s in sigs if s["type"] == "objection_shift")
    assert obj["current"]["authors"] == 6
    assert obj["intent_mix"] == {"complaint": 6}  # evidence scoped to complaint rows only
    assert obj["top_author_share"] <= CFG["detect"]["max_top_author_share"]


def test_objection_shift_respects_the_breadth_guard():
    db = fresh_db()
    run(db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                         "covered_from": NOW - timedelta(days=12), "until": NOW,
                                         "started_at": NOW}))
    run(topics.resolve(db, MARKET["key"], "bob wig", NOW - timedelta(days=30)))
    for ago in range(1, 11):
        run(_post(db, "bob wig", f"@c{ago}", "complaint", NOW - timedelta(days=ago, hours=1), "negative"))
    # today: one account posting 6 times — not 6 distinct voices
    for i in range(6):
        run(_post(db, "bob wig", "@one_loud_account", "complaint", NOW - timedelta(hours=1 + i), "negative"))
    types = {s["type"] for s in _detect(db) if s["topic"] == "bob wig"}
    assert "objection_shift" not in types


# ── cooling ─────────────────────────────────────────────────────────────────

async def _seed_rising(db, topic="latasha wig"):
    await db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                           "covered_from": NOW - timedelta(days=12), "until": NOW,
                                           "started_at": NOW})
    await topics.resolve(db, MARKET["key"], topic, NOW - timedelta(days=30))
    for ago, n in {0: 20, 1: 19, **{d: 8 for d in range(2, 11)}}.items():
        for i in range(n):
            await _post(db, topic, f"@a{ago}_{i}", "question", NOW - timedelta(days=ago, hours=1 + (i % 20)))


def test_cooling_expires_the_prior_signal_and_live_card():
    db = fresh_db()
    run(_seed_rising(db))
    first = _detect(db, NOW)
    rising = next(s for s in first if s["type"] == "rising" and s["topic"] == "latasha wig")
    assert rising["current"]["authors"] == 20

    # a live card the owner hasn't acted on yet, as personalize.cards_for
    # would have created from that rising signal
    run(db["mi_listen_cards"].insert_one({
        "id": "card-1", "brand_id": "b1", "market": MARKET["key"], "topic": "latasha wig",
        "status": "shown", "expires_at": NOW + timedelta(days=7),
    }))

    now2 = NOW + timedelta(days=1)
    run(db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                         "covered_from": NOW, "until": now2, "started_at": now2}))
    # momentum fades hard: only 3 distinct voices today, against a baseline
    # of 8/day before the spike
    for i in range(3):
        run(_post(db, "latasha wig", f"@fading{i}", "question", now2 - timedelta(hours=1 + i)))

    second = _detect(db, now2)
    cooling = [s for s in second if s["type"] == "cooling" and s["topic"] == "latasha wig"]
    assert cooling, second
    assert cooling[0]["current"]["authors"] == 3
    assert cooling[0]["current"]["peak_authors"] == 20

    stored_rising = run(db["mi_listen_signals"].find_one(
        {"market": MARKET["key"], "topic": "latasha wig", "type": "rising"}))
    assert stored_rising["expires_at"] <= now2  # expired right away, not left to run its 7-day TTL

    stored_card = run(db["mi_listen_cards"].find_one({"id": "card-1"}))
    assert stored_card["expires_at"] <= now2


def test_cooling_never_fires_while_still_rising():
    db = fresh_db()
    run(_seed_rising(db))
    first = _detect(db, NOW)
    assert any(s["type"] == "rising" for s in first)
    assert not any(s["type"] == "cooling" for s in first)


# ── evergreen protection (PRD §9.2) ──────────────────────────────────────────

async def _signal_doc(db, topic, type_, m_score, confidence=0.8):
    doc = {"id": f"sig-{topic}", "market": MARKET["key"], "market_label": "Hair & wigs", "topic": topic,
           "topic_name": topic, "type": type_, "day": "2026-10-16",
           "window_start": NOW - timedelta(hours=24), "window_end": NOW,
           "posts": 5, "intent_mix": {"purchase": 5}, "buying_mix": {"high": 5}, "needs": [],
           "top_author_share": 0.3, "reading_confidence": 0.8, "samples": [],
           "status": "active", "computed_at": NOW, "expires_at": NOW + timedelta(days=7),
           "current": {"authors": 10}, "baseline": {"median_per_day": 1.0, "days": 9},
           "z": 5.0, "growth": 1.0, "persistence_days": 3,
           "scores": {"M": m_score, "C": 70}, "confidence": confidence}
    await db["mi_listen_signals"].insert_one(dict(doc))
    return doc


PROFILE = {"brand_id": "b1", "user_id": "u1", "markets": [MARKET["key"]]}


def test_recurring_need_is_not_crowded_out_of_judging(monkeypatch):
    db = fresh_db()
    for i in range(15):  # far more trending signals than max_signals_judged (12)
        run(_signal_doc(db, f"topic{i}", "rising", m_score=90))
    run(_signal_doc(db, "frontal install", "recurring_need", m_score=30, confidence=0.6))

    captured = {}

    async def fake_judge(db, profile, sigs, model):
        captured["sigs"] = sigs
        return {}
    monkeypatch.setattr(personalize, "_judge", fake_judge)

    run(personalize.cards_for(db, PROFILE, CFG, NOW))

    assert "sigs" in captured
    judged_types = {s["type"] for s in captured["sigs"]}
    assert "recurring_need" in judged_types
    assert len(captured["sigs"]) <= CFG["score"]["max_signals_judged"]


class _J:
    def __init__(self, idx, why="Fits your catalogue and your customers are already asking."):
        self.idx, self.relevance, self.brand_fit, self.why = idx, 80, 70, why
        self.action = personalize._Action(type="post", title="Post about it", platforms=["instagram"],
                                          format="Single image post", hook="Hook", outline=["a", "b", "c"],
                                          cta="DM to order", caption="Caption")
        self.alternative = None


def test_recurring_need_card_is_not_crowded_out_of_the_final_cut(monkeypatch):
    db = fresh_db()
    for i in range(10):  # far more than max_cards_per_run (4)
        run(_signal_doc(db, f"topic{i}", "rising", m_score=95))
    run(_signal_doc(db, "frontal install", "recurring_need", m_score=30, confidence=0.6))

    async def fake_judge(db, profile, sigs, model):
        return {i: _J(i) for i in range(len(sigs))}
    monkeypatch.setattr(personalize, "_judge", fake_judge)

    cards = run(personalize.cards_for(db, PROFILE, CFG, NOW))

    assert len(cards) <= CFG["score"]["max_cards_per_run"]
    assert any(c["type"] == "recurring_need" for c in cards)


# ── upcoming_development ─────────────────────────────────────────────────

async def _seed_upcoming(db, topic="latasha wig", event_date="2026-12-01", n_authors=3, same_author=False):
    await db["mi_listen_runs"].insert_one({"market": MARKET["key"], "status": "completed",
                                           "covered_from": NOW - timedelta(days=14), "until": NOW,
                                           "started_at": NOW})
    await topics.resolve(db, MARKET["key"], topic, NOW - timedelta(days=30))
    for i in range(n_authors):
        author = "@one" if same_author else f"@fan{i}"
        await _post(db, topic, author, "trend", NOW - timedelta(days=1, hours=i), event_date=event_date)


def test_upcoming_development_fires_on_a_shared_future_date():
    db = fresh_db()
    run(_seed_upcoming(db))
    sigs = [s for s in _detect(db) if s["topic"] == "latasha wig"]
    assert "upcoming_development" in {s["type"] for s in sigs}
    up = next(s for s in sigs if s["type"] == "upcoming_development")
    assert up["current"]["authors"] == 3
    assert up["current"]["event_date"] == "2026-12-01"
    assert up["current"]["days_until"] == (datetime(2026, 12, 1).date() - NOW.date()).days


def test_upcoming_development_ignores_a_date_already_in_the_past():
    db = fresh_db()
    run(_seed_upcoming(db, event_date="2026-09-01"))  # before NOW (2026-10-16)
    types = {s["type"] for s in _detect(db) if s["topic"] == "latasha wig"}
    assert "upcoming_development" not in types


def test_upcoming_development_needs_distinct_voices_not_one_account_repeating():
    db = fresh_db()
    run(_seed_upcoming(db, n_authors=3, same_author=True))
    types = {s["type"] for s in _detect(db) if s["topic"] == "latasha wig"}
    assert "upcoming_development" not in types


def test_observation_and_allowed_numbers_ground_the_event_date():
    sig = {"type": "upcoming_development", "topic": "latasha wig", "topic_name": "latasha wig",
           "market_label": "Hair & wigs · Nigeria",
           "current": {"authors": 3, "event_date": "2026-12-01", "days_until": 46}}
    text = personalize.observation(sig)
    assert "3 different accounts" in text and "2026-12-01" in text and "46 days" in text
    allowed = personalize._allowed_numbers(sig, {"facts": {}})
    assert {"3", "2026-12-01", "2026", "12", "01", "46"} <= allowed
