"""
Private-term collection and competitor-move signals (PRD §6.4-6.5, §8.3).

Confirmed missing in a full module audit (Oct 2026): profile.build() already
computed private_terms (brand name + named competitors) on every listening
profile, but nothing ever searched for them — Listening could not hear
about a business's own brand or its named competitors at all, and
"competitor move" had no evidence to ever be detected from. Uses an
in-memory MongoDB; no network, no real AI calls (AI steps are stubbed).
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import (
    competitor_signals, enrich, permissions, personalize, private_collect,
)
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge
from app.agents.market_intelligence.models import RawEvidence

NOW = datetime(2026, 10, 24, 12, 0)
CFG = _merge(DEFAULTS, {})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["listen_private_test"]


PROFILE = {"brand_id": "b1", "user_id": "u1", "markets": ["hair_wigs:NG"],
           "facts": {"brand_name": "LushWigs", "competitors": ["RivalWigsNG", "WigQueenLagos"]}}


# ── query compiler ───────────────────────────────────────────────────────────

def test_compile_private_queries_tags_own_brand_vs_competitor():
    qs = private_collect.compile_private_queries(
        {"own_brand": ["LushWigs"], "competitor": ["RivalWigsNG", "WigQueenLagos"]})
    by_kind = {q["kind"]: [x["term"] for x in qs if x["kind"] == q["kind"]] for q in qs}
    assert by_kind["own_brand"] == ["LushWigs"]
    assert set(by_kind["competitor"]) == {"RivalWigsNG", "WigQueenLagos"}
    assert all(len(q["id"]) == 12 for q in qs)


def test_compile_private_queries_skips_blank_and_dedupes_case_insensitively():
    qs = private_collect.compile_private_queries(
        {"own_brand": ["LushWigs", ""], "competitor": ["lushwigs", "RivalWigsNG"]})
    terms = [q["term"] for q in qs]
    assert terms == ["LushWigs", "RivalWigsNG"]  # "lushwigs" collapsed into the own_brand entry


def test_private_market_key_is_per_brand_and_stable():
    assert private_collect.private_market_key("b1") == "private:b1"
    assert private_collect.private_market_key("b1") == private_collect.private_market_key("b1")
    assert private_collect.private_market_key("b1") != private_collect.private_market_key("b2")


# ── collection ────────────────────────────────────────────────────────────────

class _Page:
    def __init__(self, evidence):
        self.evidence, self.has_more, self.next_cursor, self.gaps = evidence, False, None, []


class _FakeAdapter:
    def __init__(self, items_by_query=None, default_n=2):
        self.items_by_query = items_by_query or {}
        self.default_n = default_n
        self.queries_seen = []

    async def start_collection(self, kw, ex, since, until, items_per_query=None, location=None):
        self.queries_seen.append(kw[0])
        return f"run:{kw[0]}"

    async def fetch_page(self, run_id, cursor=None):
        q = run_id.split(":", 1)[1]
        n = self.items_by_query.get(q, self.default_n)
        return _Page([RawEvidence(provider="x", platform="x", source_id=f"{q}:{i}",
                                  text=f"post about {q} number {i}", published_at=NOW - timedelta(hours=i),
                                  collected_at=NOW, author_handle=f"@user{i}") for i in range(n)])

    async def actual_cost(self, run_id):
        return 0.001


def test_collect_private_is_blocked_without_an_approved_source():
    db = fresh_db()
    res = run(private_collect.collect_private(db, PROFILE, CFG, _FakeAdapter(), "tiktok", NOW))
    assert res["status"] == "blocked_unapproved_source"


def test_collect_private_stops_at_the_monthly_budget():
    db = fresh_db()
    run(permissions.ensure_seeded(db))
    run(db["mi_listen_runs"].insert_one({"market": "x", "started_at": NOW, "cost_usd": 999.0}))
    res = run(private_collect.collect_private(db, PROFILE, CFG, _FakeAdapter(), "x", NOW))
    assert res["status"] == "skipped_budget"


def test_collect_private_runs_one_query_per_term_never_touches_mi_markets():
    db = fresh_db()
    run(permissions.ensure_seeded(db))
    adapter = _FakeAdapter()
    res = run(private_collect.collect_private(db, PROFILE, CFG, adapter, "x", NOW))
    assert res["status"] == "completed"
    assert res["brand_id"] == "b1" and res["private"] is True
    assert res["market"] == "private:b1"
    assert set(adapter.queries_seen) == {"LushWigs", "RivalWigsNG", "WigQueenLagos"}
    # Items land in the shared store (storage is shared; the READING is not)...
    item = run(db["mi_listen_items"].find_one({"source_id": "LushWigs:0"}))
    assert item is not None
    # ...but no shared market document or subscriber list is ever touched.
    assert run(db["mi_markets"].count_documents({})) == 0
    assert len(res["to_read"]) == 6  # 2 items x 3 terms


def test_collect_private_with_no_terms_is_a_harmless_no_op():
    db = fresh_db()
    run(permissions.ensure_seeded(db))
    empty_profile = {"brand_id": "b2", "user_id": "u2", "facts": {}}
    res = run(private_collect.collect_private(db, empty_profile, CFG, _FakeAdapter(), "x", NOW))
    assert res["status"] == "completed" and res["to_read"] == []


def test_collect_private_does_not_reread_already_read_items(monkeypatch):
    db = fresh_db()
    run(permissions.ensure_seeded(db))
    monkeypatch.setattr(enrich, "read_batch", _fake_read_batch)
    adapter = _FakeAdapter(items_by_query={"LushWigs": 1}, default_n=0)
    first = run(private_collect.collect_private(db, PROFILE, CFG, adapter, "x", NOW))
    assert len(first["to_read"]) == 1
    run(private_collect.read_private(db, PROFILE, first["to_read"], CFG))
    second = run(private_collect.collect_private(db, PROFILE, CFG, adapter, "x", NOW))
    assert second["to_read"] == []  # already-read item isn't queued again


# ── reading (AI stubbed) ─────────────────────────────────────────────────────

async def _fake_read_batch(label, posts, model):
    out = {}
    for i, p in enumerate(posts):
        out[i] = enrich._Item(idx=i, on_topic=True, intent="trend", need=None,
                              sentiment="neutral", buying_intent="low", topics=[], confidence=0.8)
    return out


def test_read_private_stores_readings_scoped_to_this_brand_only(monkeypatch):
    db = fresh_db()
    monkeypatch.setattr(enrich, "read_batch", _fake_read_batch)
    run(db["mi_listen_items"].insert_one({"source_id": "RivalWigsNG:0", "text": "RivalWigsNG just launched",
                                          "author_handle": "@u0", "published_at": NOW}))
    stats = run(private_collect.read_private(
        db, PROFILE, [("RivalWigsNG:0", "RivalWigsNG", "competitor")], CFG))
    assert stats == {"read": 1, "on_topic": 1}
    reading = run(db["mi_listen_private_readings"].find_one({"source_id": "RivalWigsNG:0"}))
    assert reading["brand_id"] == "b1" and reading["term"] == "RivalWigsNG" and reading["term_kind"] == "competitor"
    # Never written to the SHARED readings collection other businesses' market reads draw from.
    assert run(db["mi_listen_readings"].count_documents({})) == 0


# ── competitor-move signal ──────────────────────────────────────────────────

async def _seed_private_reading(db, brand_id, term, source_id, author, published_at, on_topic=True, intent="trend"):
    await db["mi_listen_items"].insert_one({
        "source_id": source_id, "text": f"{term} news", "author_handle": author,
        "published_at": published_at, "url": f"https://x.com/{author}/{source_id}", "provider": "x",
    })
    await db["mi_listen_private_readings"].update_one(
        {"source_id": source_id, "brand_id": brand_id},
        {"$set": {"source_id": source_id, "brand_id": brand_id, "term": term, "term_kind": "competitor",
                  "on_topic": on_topic, "intent": intent, "need": None, "sentiment": "neutral",
                  "buying_intent": "low", "confidence": 0.8, "published_at": published_at, "author": author}},
        upsert=True)


def test_competitor_move_requires_minimum_independent_authors():
    db = fresh_db()
    run(permissions.ensure_seeded(db))

    async def go():
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:0", "@a", NOW - timedelta(days=1))
        return await competitor_signals.detect_competitor_moves(db, PROFILE, CFG, NOW)
    sigs = run(go())
    assert sigs == []  # only 1 distinct author, min_authors default is 2


def test_competitor_move_fires_with_enough_independent_evidence():
    db = fresh_db()
    run(permissions.ensure_seeded(db))

    async def go():
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:0", "@a", NOW - timedelta(days=1))
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:1", "@b", NOW - timedelta(days=2))
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:2", "@c", NOW - timedelta(days=3))
        return await competitor_signals.detect_competitor_moves(db, PROFILE, CFG, NOW)
    sigs = run(go())
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig["type"] == "competitor_move"
    assert sig["topic_name"] == "RivalWigsNG"
    assert sig["market"] == "private:b1"
    assert sig["current"]["authors"] == 3
    assert sig["samples"] and all(s["url"] for s in sig["samples"])
    assert sig["scores"]["C"] > 0
    assert 0 < sig["confidence"] <= 1.0
    # Persisted for cards_for() to pick up on a later call.
    stored = run(db["mi_listen_signals"].find_one({"market": "private:b1", "type": "competitor_move"}))
    assert stored is not None


def test_competitor_move_respects_top_author_share_breadth_guard():
    db = fresh_db()
    run(permissions.ensure_seeded(db))

    async def go():
        # 2 distinct authors clears min_authors, but one of them posted 9 of
        # 10 total items — a single loud account, not real breadth.
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:0", "@spammer", NOW - timedelta(days=1))
        for i in range(1, 9):
            await _seed_private_reading(db, "b1", "RivalWigsNG", f"r:{i}", "@spammer", NOW - timedelta(days=1))
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:9", "@real_person", NOW - timedelta(days=2))
        return await competitor_signals.detect_competitor_moves(db, PROFILE, CFG, NOW)
    sigs = run(go())
    assert sigs == []


def test_competitor_move_ignores_off_topic_readings():
    db = fresh_db()
    run(permissions.ensure_seeded(db))

    async def go():
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:0", "@a", NOW - timedelta(days=1), on_topic=False)
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:1", "@b", NOW - timedelta(days=2), on_topic=False)
        return await competitor_signals.detect_competitor_moves(db, PROFILE, CFG, NOW)
    assert run(go()) == []


def test_competitor_move_only_considers_this_brands_own_readings():
    db = fresh_db()
    run(permissions.ensure_seeded(db))

    async def go():
        await _seed_private_reading(db, "other_brand", "RivalWigsNG", "r:0", "@a", NOW - timedelta(days=1))
        await _seed_private_reading(db, "other_brand", "RivalWigsNG", "r:1", "@b", NOW - timedelta(days=2))
        return await competitor_signals.detect_competitor_moves(db, PROFILE, CFG, NOW)
    assert run(go()) == []  # PROFILE is brand_id "b1", not "other_brand"


# ── reaching the card layer ──────────────────────────────────────────────────

def test_competitor_move_signal_is_a_real_candidate_for_cards_for(monkeypatch):
    db = fresh_db()
    run(permissions.ensure_seeded(db))

    async def go():
        # 3 posts, 3 distinct authors: clears both min_authors (>=2) and the
        # top-author-share breadth guard (2 posts would always fail it, since
        # one author would always hold >=50% > the 40% cap).
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:0", "@a", NOW - timedelta(days=1))
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:1", "@b", NOW - timedelta(days=2))
        await _seed_private_reading(db, "b1", "RivalWigsNG", "r:2", "@c", NOW - timedelta(days=3))
        await competitor_signals.detect_competitor_moves(db, PROFILE, CFG, NOW)

    run(go())

    async def fake_judge(db, profile, sigs, model):
        from app.agents.market_intelligence.listening.personalize import _Judged, _Action
        out = {}
        for i, s in enumerate(sigs):
            out[i] = _Judged(idx=i, relevance=80, brand_fit=80, why="Your competitor moved; respond in your own way.",
                             action=_Action(type="post", title="Show what makes us different", platforms=["instagram"],
                                           format="Single image post", hook="Here's our take.",
                                           outline=["Point 1", "Point 2"], cta="DM us.",
                                           caption="We do things differently."))
        return out
    monkeypatch.setattr(personalize, "_judge", fake_judge)
    cards = run(personalize.cards_for(db, PROFILE, CFG, NOW))
    assert len(cards) == 1
    assert cards[0]["type"] == "competitor_move"
    assert "RivalWigsNG" in cards[0]["observation"]
    assert cards[0]["market"] == "private:b1"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
