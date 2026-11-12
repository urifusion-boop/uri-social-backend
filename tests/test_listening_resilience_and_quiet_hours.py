"""
Two gaps closed together (PRD §8 stage 3 "basic filters... spam/bot/promo
patterns... drops most noise free, before any AI" and PRD §5/§10's
"Operating policy: ... quiet hours"):

1. enrich.read_batch now runs the same deterministic spam/bot filter the
   older Market Intelligence pipeline already proved (noise_filter.py)
   before spending an AI call, and retries once on a rate-limited AI
   response instead of silently dropping the whole batch.
2. personalize.notify now checks the same per-business quiet-hours
   preference (mi_preferences) the older Market Intelligence insights
   feature already uses, so an immediate alert doesn't ping an owner at
   night; the card itself stays live in-app either way.
3. enrich.read_batch now also tells the model each post's own posted-date,
   so it can resolve relative future dates ("next month") into the
   event_date field upcoming_development (detect.py) needs.

Uses an in-memory MongoDB; no real AI calls (AIService is stubbed).
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence import notification_delivery
from app.agents.market_intelligence.listening import enrich, personalize
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge
from app.services.AIService import AIService

CFG = _merge(DEFAULTS, {})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["listen_resilience_test"]


def _no_sleep(monkeypatch):
    async def fake_sleep(_seconds):
        return None
    monkeypatch.setattr(enrich.asyncio, "sleep", fake_sleep)


class _FakeCompletion:
    def __init__(self, items):
        class _Msg:
            def __init__(self, parsed):
                self.parsed = parsed
        class _Choice:
            def __init__(self, parsed):
                self.message = _Msg(parsed)
        self.choices = [_Choice(enrich._Batch(items=items))]


# ── spam/bot filter (PRD §8 stage 3) ─────────────────────────────────────────

def test_spam_pattern_never_reaches_ai(monkeypatch):
    calls = []

    async def fake_ai(req, response_model=None):
        calls.append(req)
        return _FakeCompletion([enrich._Item(idx=0, on_topic=True, intent="praise", need=None,
                                             sentiment="positive", buying_intent="low", topics=[], confidence=0.9)])
    monkeypatch.setattr(AIService, "structured_chat_completion", fake_ai)

    posts = [{"text": "DM for rates now, limited time offer"}, {"text": "I love this new skincare routine"}]
    out = run(enrich.read_batch("skincare", posts))

    assert len(calls) == 1
    sent_block = calls[0].messages[-1].content
    assert "limited time offer" not in sent_block
    assert "skincare routine" in sent_block
    assert out[0].on_topic is False and out[0].confidence == 1.0
    assert out[1].on_topic is True and out[1].idx == 1


def test_all_posts_noisy_never_calls_ai(monkeypatch):
    calls = []

    async def fake_ai(req, response_model=None):
        calls.append(req)
        return _FakeCompletion([])
    monkeypatch.setattr(AIService, "structured_chat_completion", fake_ai)

    posts = [{"text": "click link in bio for crypto gains"}, {"text": "!!!!!!!!!!!!"}]
    out = run(enrich.read_batch("skincare", posts))

    assert calls == []
    assert out[0].on_topic is False
    assert out[1].on_topic is False


# ── rate-limit retry (matches the Content Calendar fix) ─────────────────────

def test_rate_limited_call_retries_once_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    attempts = []

    async def fake_ai(req, response_model=None):
        attempts.append(1)
        if len(attempts) == 1:
            return {"error": "AI service is temporarily unavailable. Please try again later or contact support."}
        return _FakeCompletion([enrich._Item(idx=0, on_topic=True, intent="question", need="pricing",
                                             sentiment="neutral", buying_intent="medium", topics=[], confidence=0.7)])
    monkeypatch.setattr(AIService, "structured_chat_completion", fake_ai)

    out = run(enrich.read_batch("skincare", [{"text": "how much does this cost"}]))

    assert len(attempts) == 2
    assert out[0].on_topic is True
    assert out[0].need == "pricing"


def test_rate_limited_call_fails_both_attempts_leaves_only_noise_items(monkeypatch):
    _no_sleep(monkeypatch)
    attempts = []

    async def fake_ai(req, response_model=None):
        attempts.append(1)
        return {"error": "AI service is temporarily unavailable. Please try again later or contact support."}
    monkeypatch.setattr(AIService, "structured_chat_completion", fake_ai)

    posts = [{"text": "inbox me for the sugar daddy program"}, {"text": "how much does this cost"}]
    out = run(enrich.read_batch("skincare", posts))

    assert len(attempts) == 2  # one clean post, one retry — the noisy post never made an attempt
    assert 0 in out and out[0].on_topic is False  # deterministic noise reading still present
    assert 1 not in out  # "Missing idx = could not be read (left for the next run)"


# ── quiet hours (PRD §5 "Operating policy", §10 fatigue control) ────────────

def _card(score=90):
    return {"id": "card-1", "market": "hair_wigs:NG", "topic": "latasha wig", "topic_name": "latasha wig",
            "type": "post", "observation": "People are asking about latasha wigs.",
            "why": "Several posts asked where to buy one this week.",
            "action": {"title": "Post about latasha wig availability"}, "scores": {"total": score}}


def _notify_spy(monkeypatch, calls):
    async def fake_notify(db, user_id, brand_id, event, **kwargs):
        calls.append({"user_id": user_id, "brand_id": brand_id, "event": event, **kwargs})
        return {"sent": True}
    from app.services import brand_notifier
    monkeypatch.setattr(brand_notifier, "notify", fake_notify)


def _prefs_spy(monkeypatch, prefs):
    async def fake_prefs(db, user_id, brand_id):
        return dict(prefs)
    monkeypatch.setattr(notification_delivery, "get_or_create_preferences", fake_prefs)


def test_notify_suppressed_during_quiet_hours_but_card_stays_untouched(monkeypatch):
    db = fresh_db()
    run(db["mi_listen_cards"].insert_one(dict(_card())))
    calls = []
    _notify_spy(monkeypatch, calls)
    _prefs_spy(monkeypatch, {"quiet_hours_start_local": 21, "quiet_hours_end_local": 8, "urgent_override": False})

    now = datetime(2026, 10, 25, 23, 0)  # UTC hour 23 — within the default 21-8 quiet window
    sent = run(personalize.notify(db, {"brand_id": "b1", "user_id": "u1"}, [_card()], CFG, now))

    assert sent == 0
    assert calls == []
    stored = run(db["mi_listen_cards"].find_one({"id": "card-1"}))
    assert stored.get("alerted_at") is None  # card is still live in-app, just not pinged


def test_urgent_override_bypasses_quiet_hours(monkeypatch):
    db = fresh_db()
    run(db["mi_listen_cards"].insert_one(dict(_card())))
    calls = []
    _notify_spy(monkeypatch, calls)
    _prefs_spy(monkeypatch, {"quiet_hours_start_local": 21, "quiet_hours_end_local": 8, "urgent_override": True})

    now = datetime(2026, 10, 25, 23, 0)
    sent = run(personalize.notify(db, {"brand_id": "b1", "user_id": "u1"}, [_card()], CFG, now))

    assert sent == 1
    assert len(calls) == 1


def test_notify_sends_normally_outside_quiet_hours(monkeypatch):
    db = fresh_db()
    run(db["mi_listen_cards"].insert_one(dict(_card())))
    calls = []
    _notify_spy(monkeypatch, calls)
    _prefs_spy(monkeypatch, {"quiet_hours_start_local": 21, "quiet_hours_end_local": 8, "urgent_override": False})

    now = datetime(2026, 10, 25, 14, 0)  # clearly daytime
    sent = run(personalize.notify(db, {"brand_id": "b1", "user_id": "u1"}, [_card()], CFG, now))

    assert sent == 1
    assert len(calls) == 1
    stored = run(db["mi_listen_cards"].find_one({"id": "card-1"}))
    assert stored.get("alerted_at") == now


# ── upcoming_development's event_date extraction (PRD §8.3) ────────────────

def test_each_post_s_own_posted_date_is_given_so_relative_dates_resolve(monkeypatch):
    captured = {}

    async def fake_ai(req, response_model=None):
        captured["prompt"] = req.messages[-1].content
        return _FakeCompletion([enrich._Item(idx=0, on_topic=True, intent="trend", need=None, sentiment="neutral",
                                             buying_intent="low", topics=[], event_date="2026-12-01", confidence=0.8)])
    monkeypatch.setattr(AIService, "structured_chat_completion", fake_ai)

    posts = [{"text": "launching our new store next month", "published_at": datetime(2026, 11, 1)}]
    out = run(enrich.read_batch("skincare", posts))

    assert "(posted 2026-11-01)" in captured["prompt"]
    assert out[0].event_date == "2026-12-01"


def test_missing_published_at_does_not_crash_the_prompt(monkeypatch):
    async def fake_ai(req, response_model=None):
        return _FakeCompletion([enrich._Item(idx=0, on_topic=True, intent="other", need=None, sentiment="neutral",
                                             buying_intent="none", topics=[], confidence=0.5)])
    monkeypatch.setattr(AIService, "structured_chat_completion", fake_ai)

    out = run(enrich.read_batch("skincare", [{"text": "no date on this one"}]))
    assert out[0].event_date is None
