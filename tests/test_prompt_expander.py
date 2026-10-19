"""Prompt expander: a short idea becomes a brand-guided brief (AI stubbed)."""
import asyncio
from datetime import datetime

from app.agents.social_media_manager.services import prompt_expander as pe

BC = {
    "brand_name": "Glow Hair", "industry": "Hair & wigs", "business_description": "Premium human-hair wigs",
    "key_products_services": ["frontal wigs", "bob wigs", ""], "target_audience": "Lagos women 25-40",
    "brand_voice": "warm, confident, playful", "words_to_avoid": ["cheap"], "primary_goal": "WhatsApp enquiries",
    "cta_styles": ["DM us"], "brand_colors": ["#B0185A"], "guardrails": {"claims": "no 'guaranteed' results"},
    "style_selections": ["not_a_real_style"], "price_range": "", "customer_pain_points": [],
}


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_only_filled_playbook_facts_are_used():
    f = pe.brand_facts(BC, ["afro_glam_unknown"])
    assert f["brand"] == "Glow Hair" and f["products/services"] == ["frontal wigs", "bob wigs"]
    assert f["words to avoid"] == ["cheap"] and "claims" in f["guardrails"]
    assert "price range" not in f and "customer pain points" not in f       # empty ones dropped
    assert f["visual style"] == ["afro glam unknown"]                       # unknown slug shown readably


def test_known_style_is_described(monkeypatch):
    from app.agents.social_media_manager.services import v3_style_library as lib
    slug = next(iter(lib.V3_STYLES))
    line = pe._style_lines([slug])[0]
    assert line.startswith(lib.V3_STYLES[slug].name + ":")


def test_messages_carry_idea_settings_and_rules():
    msgs = pe.build_messages("happy mothers day post", BC, ["instagram"], "carousel", 5, True, ["x_style"],
                             datetime(2026, 10, 19))
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "Never invent products" in system and "never state a date" in system
    assert "happy mothers day post" in user and "carousel with 5 slides" in user and "Images: yes" in user
    assert "Monday 19 October 2026" in user and "Glow Hair" in user and "x style" in user
    text_only = pe.build_messages("idea", BC, [], "feed", None, False, ["x_style"], datetime(2026, 10, 19))[1]["content"]
    assert "visual style" not in text_only                                  # no visual direction without images


class _Msg:
    def __init__(self, parsed):
        self.parsed, self.refusal = parsed, None


class _Comp:
    def __init__(self, parsed):
        self.choices = [type("C", (), {"message": _Msg(parsed)})()]


def test_expand_cleans_and_caps(monkeypatch):
    long = "**Write** a warm Mother's Day carousel. " + ("Celebrate mums with frontal wigs. " * 120)

    async def fake(req, response_model=None):
        assert req.model == pe.MODEL
        return _Comp(pe._Expanded(expanded_prompt=long, used=["brand voice", "products", "goal", "x", "y", "z", "w"]))
    monkeypatch.setattr(pe.AIService, "structured_chat_completion", fake)
    out = run(pe.expand("mothers day", BC, ["instagram"], "carousel", 5, True))
    assert "**" not in out["expanded_prompt"] and len(out["expanded_prompt"]) <= pe.MAX_CHARS
    assert out["expanded_prompt"].endswith(".") and len(out["used"]) == 6


def test_ai_failure_returns_none(monkeypatch):
    async def boom(req, response_model=None):
        raise RuntimeError("down")
    monkeypatch.setattr(pe.AIService, "structured_chat_completion", boom)
    assert run(pe.expand("independence day post", BC, ["facebook"])) is None


def test_markdown_links_become_plain_text(monkeypatch):
    async def fake(req, response_model=None):
        return _Comp(pe._Expanded(expanded_prompt="End with a CTA to visit [www.urisocial.com](http://www.urisocial.com) today.",
                                  used=[]))
    monkeypatch.setattr(pe.AIService, "structured_chat_completion", fake)
    out = run(pe.expand("tips post", BC, ["linkedin"]))
    assert out["expanded_prompt"] == "End with a CTA to visit www.urisocial.com today."


def test_greeting_and_vague_idea_rules_are_sent():
    system = pe.build_messages("x", BC, [], "feed", None, False, [], datetime(2026, 10, 19))[0]["content"]
    assert "brand is simply the warm sender" in system and "choose ONE specific, useful angle" in system
