"""
Two more PRD gaps closed together, both building on the new embeddings
module (app/agents/market_intelligence/listening/embeddings.py):

1. Topic graph edges (PRD §8.1): an embedding per topic (computed once, on
   creation) and co-occurrence counts between topics mentioned in the same
   post.
2. Relevance as discrete components (PRD §9.1): semantic similarity
   between the business profile embedding and a signal's topic embedding,
   and language fit between the business's own languages and the market's
   — both able to only ever REDUCE a card's relevance, never inflate it
   past what the AI judge decided.

conftest.py's autouse fixture already fakes AIService.create_embedding
(deterministic, zero network) for every test; these tests monkeypatch the
higher-level embeddings module directly where a specific, controlled
vector (not just "some fake vector") is needed for a deterministic
assertion.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import embeddings, personalize, profile as profile_mod, topics
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 16, 12, 0)
CFG = _merge(DEFAULTS, {})
MARKET_KEY = "hair_wigs:NG"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["listen_relevance_test"]


# ── embeddings.py ────────────────────────────────────────────────────────────

def test_cosine_similarity_of_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert embeddings.cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero():
    assert embeddings.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_is_none_without_both_vectors():
    assert embeddings.cosine_similarity(None, [1.0]) is None
    assert embeddings.cosine_similarity([1.0], None) is None
    assert embeddings.cosine_similarity([1.0, 2.0], [1.0]) is None  # mismatched length


def test_embed_text_returns_none_for_blank_text():
    assert run(embeddings.embed_text("")) is None
    assert run(embeddings.embed_text("   ")) is None


def test_embed_text_degrades_to_none_on_failure(monkeypatch):
    async def boom(req):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(embeddings.AIService, "create_embedding", boom)
    assert run(embeddings.embed_text("latasha wig")) is None


# ── topic graph: embedding once per topic, co-occurrence edges ─────────────

def test_topic_embedding_is_computed_once_on_creation_only(monkeypatch):
    calls = []

    async def fake_embed(text):
        calls.append(text)
        return [0.1, 0.2, 0.3]
    monkeypatch.setattr(topics.embeddings, "embed_text", fake_embed)

    db = fresh_db()
    run(topics.resolve(db, MARKET_KEY, "latasha wig", NOW))       # new -> embeds
    run(topics.resolve(db, MARKET_KEY, "Latasha Wigs", NOW))      # same topic, different wording -> no re-embed
    run(topics.resolve(db, MARKET_KEY, "bob wig", NOW))           # a second new topic -> embeds

    assert calls == ["latasha wig", "bob wig"]
    doc = run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": "latasha wig"}))
    assert doc["embedding"] == [0.1, 0.2, 0.3]


def test_topic_without_a_successful_embedding_still_gets_created(monkeypatch):
    async def fails(text):
        return None
    monkeypatch.setattr(topics.embeddings, "embed_text", fails)
    db = fresh_db()
    key = run(topics.resolve(db, MARKET_KEY, "latasha wig", NOW))
    assert key == "latasha wig"
    doc = run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": "latasha wig"}))
    assert "embedding" not in doc or doc.get("embedding") is None


async def _post_with_topics(db, topic_list, ts):
    sid = f"x:{ts.isoformat()}:{','.join(topic_list)}"
    item = {"source_id": sid, "author_handle": "@a", "published_at": ts, "raw_metrics": {},
            "text": "about " + " and ".join(topic_list)}
    reading = {"intent": "question", "buying_intent": "low", "topics": topic_list}
    for t in topic_list:
        await topics.count(db, MARKET_KEY, t, item, reading)


def test_topics_mentioned_together_get_a_co_occurrence_edge(monkeypatch):
    async def fake_embed(text):
        return None
    monkeypatch.setattr(topics.embeddings, "embed_text", fake_embed)
    db = fresh_db()
    run(topics.resolve(db, MARKET_KEY, "latasha wig", NOW))
    run(topics.resolve(db, MARKET_KEY, "frontal", NOW))
    run(_post_with_topics(db, ["latasha wig", "frontal"], NOW))
    run(_post_with_topics(db, ["latasha wig", "frontal"], NOW + timedelta(hours=1)))

    latasha = run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": "latasha wig"}))
    frontal = run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": "frontal"}))
    assert latasha["co_occurs"] == {"frontal": 2}
    assert frontal["co_occurs"] == {"latasha wig": 2}  # the edge is recorded from both sides


def test_a_single_topic_post_gets_no_co_occurrence_edge():
    db = fresh_db()
    run(topics.resolve(db, MARKET_KEY, "latasha wig", NOW))
    run(_post_with_topics(db, ["latasha wig"], NOW))
    doc = run(db["mi_market_topics"].find_one({"market": MARKET_KEY, "key": "latasha wig"}))
    assert "co_occurs" not in doc


# ── profile embedding (PRD §9.1's "business profile embedding") ───────────

def test_profile_build_computes_and_caches_an_embedding(monkeypatch):
    db = fresh_db()

    class _Msg:
        def __init__(self, parsed):
            self.parsed, self.refusal = parsed, None

    class _Completion:
        def __init__(self, parsed):
            self.choices = [type("C", (), {"message": _Msg(parsed)})()]

    async def fake(req, response_model=None):
        return _Completion(profile_mod._MapLLM(category_ids=["hair_wigs"], new_category=None, country="NG"))
    monkeypatch.setattr(profile_mod.AIService, "structured_chat_completion", fake)

    playbook = {"brand_name": "LushWigs", "industry": "Hair and beauty",
                "product_description": "Human hair wigs", "customer_location": "Lagos"}
    prof1 = run(profile_mod.build(db, "u1", "b1", playbook))
    assert prof1["profile_embedding"] and isinstance(prof1["profile_embedding"], list)
    assert prof1["field_origins"]["profile_embedding"] == "ai_mapping"

    embed_calls = []
    monkeypatch.setattr(profile_mod.embeddings, "embed_text",
                        lambda text: embed_calls.append(text) or _const_vec())
    prof2 = run(profile_mod.build(db, "u1", "b1", playbook))  # unchanged -> fingerprint short-circuit
    assert embed_calls == []  # never recomputed when nothing actually changed
    assert prof2["profile_embedding"] == prof1["profile_embedding"]


async def _const_vec():
    return [0.5, 0.5]


# ── relevance components (PRD §9.1) ─────────────────────────────────────────

def _sig(market=MARKET_KEY, topic="latasha wig"):
    return {"market": market, "topic": topic, "scores": {"M": 70, "C": 60}, "confidence": 0.8}


def test_structural_components_are_always_full_and_exclusions_always_clear():
    db = fresh_db()
    components = run(personalize._relevance_components(db, _sig(), {"facts": {}}))
    assert components["ontology_match"] == 100.0
    assert components["geography_fit"] == 100.0
    assert components["exclusions_clear"] == 100.0


def test_semantic_similarity_is_none_without_both_embeddings():
    db = fresh_db()
    components = run(personalize._relevance_components(db, _sig(), {"facts": {}}))
    assert components["semantic_similarity"] is None
    assert personalize._component_adjustment(components) == 0.0


def test_low_semantic_similarity_reduces_relevance(monkeypatch):
    db = fresh_db()
    run(db["mi_market_topics"].insert_one({"market": MARKET_KEY, "key": "latasha wig", "embedding": [1.0, 0.0]}))
    profile = {"facts": {}, "profile_embedding": [0.0, 1.0]}  # orthogonal -> similarity 0

    components = run(personalize._relevance_components(db, _sig(), profile))
    assert components["semantic_similarity"] == 0.0
    assert personalize._component_adjustment(components) < 0


def test_high_semantic_similarity_never_boosts_relevance():
    db = fresh_db()
    run(db["mi_market_topics"].insert_one({"market": MARKET_KEY, "key": "latasha wig", "embedding": [1.0, 0.0]}))
    profile = {"facts": {}, "profile_embedding": [1.0, 0.0]}  # identical -> similarity 1

    components = run(personalize._relevance_components(db, _sig(), profile))
    assert components["semantic_similarity"] == 100.0
    assert personalize._component_adjustment(components) == 0.0  # only ever non-positive


def test_language_mismatch_reduces_relevance_match_does_not():
    db = fresh_db()
    fr_only = run(personalize._relevance_components(db, _sig(), {"facts": {"languages": ["fr"]}}))
    assert fr_only["language_fit"] == 40.0
    assert personalize._component_adjustment(fr_only) == -10.0

    en_business = run(personalize._relevance_components(db, _sig(), {"facts": {"languages": ["en"]}}))
    assert en_business["language_fit"] == 100.0
    assert personalize._component_adjustment(en_business) == 0.0


# ── end to end through cards_for() ──────────────────────────────────────────

class _J:
    def __init__(self, idx, rel=80):
        self.idx, self.relevance, self.brand_fit = idx, rel, 70
        self.why = "Fits your catalogue and your customers are already asking."
        self.action = personalize._Action(type="post", title="Post about it", platforms=["instagram"],
                                          format="Single image post", hook="Hook", outline=["a", "b", "c"],
                                          cta="DM to order", caption="Caption")
        self.alternative = None


def test_a_mismatched_language_can_push_a_card_below_the_relevance_gate(monkeypatch):
    db = fresh_db()
    sig = {"id": "sig-1", "market": MARKET_KEY, "market_label": "Hair & wigs", "topic": "latasha wig",
           "topic_name": "latasha wig", "type": "rising", "day": "2026-10-16",
           "window_start": NOW - timedelta(hours=24), "window_end": NOW,
           "posts": 5, "intent_mix": {"purchase": 5}, "buying_mix": {"high": 5}, "needs": [],
           "top_author_share": 0.3, "reading_confidence": 0.8, "samples": [],
           "status": "active", "computed_at": NOW, "expires_at": NOW + timedelta(days=7),
           "current": {"authors": 10}, "baseline": {"median_per_day": 1.0, "days": 9},
           "z": 5.0, "growth": 1.0, "persistence_days": 3, "scores": {"M": 60, "C": 60}, "confidence": 0.8}
    run(db["mi_listen_signals"].insert_one(dict(sig)))

    async def fake_judge(db, profile, sigs, model, connected=None):
        return {i: _J(i, rel=CFG["score"]["min_relevance"] + 5) for i in range(len(sigs))}  # just above the gate
    monkeypatch.setattr(personalize, "_judge", fake_judge)

    profile_en = {"brand_id": "b1", "user_id": "u1", "markets": [MARKET_KEY], "facts": {"languages": ["en"]}}
    profile_fr = {"brand_id": "b2", "user_id": "u2", "markets": [MARKET_KEY], "facts": {"languages": ["fr"]}}

    cards_en = run(personalize.cards_for(db, profile_en, CFG, NOW))
    assert len(cards_en) == 1  # language matches -> no penalty -> clears the gate

    cards_fr = run(personalize.cards_for(db, profile_fr, CFG, NOW))
    assert cards_fr == []  # -10 language penalty drops it just below min_relevance
