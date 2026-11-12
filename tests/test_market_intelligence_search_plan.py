"""Topic search plan (variation groups) and term learning."""
from types import SimpleNamespace

from app.agents.market_intelligence import query_planner as qp
from app.agents.market_intelligence import term_learning as tl
from app.agents.market_intelligence.models import EvidenceType


def _topic(**kw):
    base = dict(question="q", keywords=["Glowup", "vitamin c serum"], competitors=["Nivea"], excluded_keywords=["giveaway"],
                search_plan={"groups": [{"label": "Asking", "terms": ["recommend a serum", "glowup", "dark spot cream"]}],
                             "excluded_terms": ["minecraft"]},
                learned_terms=["skin wey dey glow"], retired_terms=["dark spot cream", "glowup"])
    base.update(kw)
    return SimpleNamespace(**base)


def test_owner_terms_are_kept_learned_added_retired_dropped():
    plan = qp.assemble(_topic())
    labels = [g["label"] for g in plan["groups"]]
    assert labels[0] == "Your keywords" and "Competitors" in labels and "Learned from real posts" in labels
    terms = plan["terms"]
    assert "glowup" in terms                       # owner keyword survives a retirement attempt
    assert "dark spot cream" not in terms          # retired AI term is gone
    assert terms.count("glowup") == 1              # no duplicates across groups
    assert all(e.startswith("(") and e.endswith(")") for e in plan["expressions"])
    assert '"vitamin c serum"' in plan["expressions"][0]
    assert set(plan["excluded"]) == {"giveaway", "minecraft"}


def test_term_matching_needs_every_word():
    assert qp.term_matches("vitamin c serum", "Best Vitamin-C serum for dark skin?")
    assert qp.term_matches("glowup", "#glowup season")
    assert not qp.term_matches("vitamin c serum", "vitamin tablets")


def test_junk_terms_retire_but_protected_ones_never_do():
    stats = {"serum": {"seen": 40, "useful": 2}, "glowup": {"seen": 40, "useful": 0},
             "sunscreen": {"seen": 5, "useful": 0}, "spf": {"seen": 30, "useful": 12}}
    out = tl.retire_candidates(stats, protected={"glowup"})
    assert out == ["serum"]   # glowup protected; sunscreen not judged yet (too few); spf useful


def test_off_topic_and_idle_chat_are_not_useful():
    on = SimpleNamespace(primary_type=EvidenceType.PURCHASE_INQUIRY, on_topic=True)
    off = SimpleNamespace(primary_type=EvidenceType.PURCHASE_INQUIRY, on_topic=False)
    chat = SimpleNamespace(primary_type=EvidenceType.GENERAL_DISCUSSION, on_topic=True)
    assert tl.is_useful(on) and not tl.is_useful(off) and not tl.is_useful(chat) and not tl.is_useful(None)
