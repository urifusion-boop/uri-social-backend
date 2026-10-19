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
    "platform_tones": {"instagram": "playful and visual", "linkedin": "professional"}, "same_tone_everywhere": False,
    "key_dates": "Store anniversary: 12 March",
}
TODAY = datetime(2026, 10, 19)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_only_filled_playbook_facts_are_used():
    f = pe.brand_facts(BC, ["afro_glam_unknown"], ["instagram"])
    assert f["brand"] == "Glow Hair" and f["products/services"] == ["frontal wigs", "bob wigs"]
    assert f["words to avoid"] == ["cheap"] and "claims" in f["guardrails"]
    assert "price range" not in f and "customer pain points" not in f       # empty ones dropped
    assert f["visual style"] == ["afro glam unknown"]                       # unknown slug shown readably
    assert f["platform tone"] == ["instagram: playful and visual"]          # only the chosen platform's tone
    assert "anniversary" in f["brand's own key dates"]


def test_same_tone_everywhere_skips_platform_tones():
    assert "platform tone" not in pe.brand_facts({**BC, "same_tone_everywhere": True}, [], ["instagram"])


def test_known_style_is_described():
    from app.agents.social_media_manager.services import v3_style_library as lib
    slug = next(iter(lib.V3_STYLES))
    assert pe._style_lines([slug])[0].startswith(lib.V3_STYLES[slug].name + ":")


def test_every_post_gets_visual_direction_and_all_settings_are_connected():
    msgs = pe.build_messages("happy mothers day post", BC, ["instagram"], "carousel", 5, ["x_style"], TODAY,
                             cta="Send us a WhatsApp message", photos=["frontal wig on model", ""])
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "EVERY post has an image" in system and "never call the post text-only" in system
    assert "Never compare the occasion to the product" in system
    assert "every post gets an image" in user and "text only" not in user.lower()
    assert "visual style: x style" in user and "brand colours: #B0185A" in user   # style even with images off
    assert "carousel with 5 slides" in user and "Monday 19 October 2026" in user
    assert "Call to action to use exactly: Send us a WhatsApp message" in user
    assert "Uploaded photos to feature (2): frontal wig on model; unlabelled photo" in user
    assert "happy mothers day post" in user and "Glow Hair" in user


class _Msg:
    def __init__(self, parsed):
        self.parsed, self.refusal = parsed, None


class _Comp:
    def __init__(self, parsed):
        self.choices = [type("C", (), {"message": _Msg(parsed)})()]


def _fake(concept, details, used=None):
    async def fake(req, response_model=None):
        assert req.model == pe.MODEL
        return _Comp(pe._Expanded(concept=concept, details=details, used=used or []))
    return fake


def test_brief_opens_with_the_concept_for_image_generation(monkeypatch):
    monkeypatch.setattr(pe.AIService, "structured_chat_completion", _fake(
        "Jane, create a Mother's Day greeting celebrating Lagos mums - image: a mother and daughter laughing at home",
        "Keep it warm. The post is text-only, so focus on words. Use the brand colours."))
    out = run(pe.expand("mothers day", BC, ["instagram"]))["expanded_prompt"]
    assert out.startswith("Mother's Day greeting celebrating Lagos mums")      # lead-in filler removed
    assert "text-only" not in out and "Keep it warm." in out
    assert len(out.split(".")[0]) <= pe.CONCEPT_MAX


def test_long_concept_and_brief_are_capped(monkeypatch):
    monkeypatch.setattr(pe.AIService, "structured_chat_completion",
                        _fake("A " + "very " * 120 + "long concept", "**Celebrate** mums. " * 200, ["a"] * 9))
    out = run(pe.expand("mothers day", BC, ["instagram"]))
    assert len(out["expanded_prompt"]) <= pe.MAX_CHARS and "**" not in out["expanded_prompt"]
    assert len(out["used"]) == 6


def test_markdown_links_become_plain_text(monkeypatch):
    monkeypatch.setattr(pe.AIService, "structured_chat_completion", _fake(
        "Tips for posting consistently with no time", "End with a CTA to visit [www.urisocial.com](http://www.urisocial.com) today."))
    out = run(pe.expand("tips post", BC, ["linkedin"]))["expanded_prompt"]
    assert out.endswith("End with a CTA to visit www.urisocial.com today.")


def test_ai_failure_returns_none(monkeypatch):
    async def boom(req, response_model=None):
        raise RuntimeError("down")
    monkeypatch.setattr(pe.AIService, "structured_chat_completion", boom)
    assert run(pe.expand("independence day post", BC, ["facebook"])) is None
