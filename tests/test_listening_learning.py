"""
Learned relevance adjustment (PRD §9.1: "a learned adjustment from that
business's past feedback on similar signals"; §11's feedback table). Card
actions were already logged to mi_listen_card_actions but nothing ever read
them back into scoring — this is what closes that loop. "Similar" means
same market + same signal type, since there's no embedding index yet.
Approving/saving never raises relevance (§11: "not treated as proof of
commercial value") — only a sustained dismiss pattern lowers it.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import personalize
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET_KEY = "hair_wigs:NG"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["learning_test"]


async def _actions(db, brand_id, market, kind, dismissed=0, saved=0, acted=0, days_ago=5):
    i = 0
    for action, n in (("dismissed", dismissed), ("saved", saved), ("acted", acted)):
        for _ in range(n):
            i += 1
            await db["mi_listen_card_actions"].insert_one({
                "card_id": f"c{i}", "brand_id": brand_id, "market": market, "type": kind,
                "action": action, "at": NOW - timedelta(days=days_ago),
            })


def _adj(db, brand_id="b1", market=MARKET_KEY, kind="rising"):
    return run(personalize._relevance_adjustment(db, brand_id, market, kind, CFG, NOW))


# ── the adjustment itself ────────────────────────────────────────────────────

def test_too_few_samples_gives_no_adjustment():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=2))  # below min_feedback_samples=4
    assert _adj(db) == 0.0


def test_mostly_approved_gives_no_adjustment():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=1, saved=4, acted=1))
    assert _adj(db) == 0.0


def test_sustained_dismissal_lowers_relevance():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=8, saved=1))  # 8/9 ~ 89% dismiss rate
    adj = _adj(db)
    assert adj < 0


def test_full_dismissal_hits_the_max_penalty():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=6))
    assert _adj(db) == -CFG["learning"]["max_penalty"]


def test_feedback_is_scoped_to_market_and_signal_type():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=6))
    assert _adj(db, kind="recurring_need") == 0.0  # different type, no shared history
    assert _adj(db, market="beauty_skincare:NG") == 0.0  # different market, no shared history


def test_feedback_is_scoped_to_the_business():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=6))
    assert _adj(db, brand_id="b2") == 0.0  # another business's dismissals never leak across


def test_old_feedback_outside_the_window_does_not_count():
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=6, days_ago=90))  # past 60-day window
    assert _adj(db) == 0.0


# ── wired into cards_for ─────────────────────────────────────────────────────

class _J:
    def __init__(self, idx, rel, fit, why="Fits your catalogue and your customers are already asking."):
        self.idx, self.relevance, self.brand_fit, self.why = idx, rel, fit, why
        self.action = personalize._Action(type="video", title="Film 3 ways to style it",
                                          platforms=["instagram"], format="Reel 30s", hook="Hook",
                                          outline=["a", "b", "c"], cta="DM to order", caption="Caption")


def test_sustained_dismissal_history_gates_an_otherwise_qualifying_card_out(monkeypatch):
    db = fresh_db()
    run(_actions(db, "b1", MARKET_KEY, "rising", dismissed=6))  # max penalty: -25
    sig = {"id": "s1", "market": MARKET_KEY, "market_label": "Hair & wigs · Nigeria", "topic": "latasha wig",
           "topic_name": "latasha wig", "type": "rising", "scores": {"M": 70, "C": 60}, "confidence": 0.8,
           "needs": [], "samples": [], "expires_at": NOW + timedelta(days=7)}

    async def fake_judge(profile, sigs, model):
        return {0: _J(0, CFG["score"]["min_relevance"] + 5, 80)}  # would pass on relevance alone
    monkeypatch.setattr(personalize, "_judge", fake_judge)

    # cards_for pulls candidates straight from mi_listen_signals — seed it directly.
    run(db["mi_listen_signals"].insert_one({**sig, "status": "active", "computed_at": NOW}))
    prof = {"brand_id": "b1", "user_id": "u1", "markets": [MARKET_KEY], "facts": {}}

    cards = run(personalize.cards_for(db, prof, CFG, NOW))

    assert cards == []  # relevance (min_relevance+5) - 25 penalty falls below the gate


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
