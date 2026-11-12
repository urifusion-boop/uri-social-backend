"""
The caption for a finished video is about THAT video, written in the brand's voice under the Playbook's rules.
"""
import asyncio
import sys
import types as pytypes
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents.social_media_manager.routers import complete_social_manager as R
from app.agents.social_media_manager.services import video_caption as vc
from app.agents.social_media_manager.services import video_storyboard_service as vss
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

PROFILE = {
    "brand_name": "Uri Social",
    "derived_voice": "warm",
    "guardrails": {"banned_words": "cheap", "emoji_usage": "no", "max_hashtags": "2"},
    "cta_styles": ["Try Uri Social today"],
    "customer_pain_points": ["lack of time"],
    "product_description": "AI sales and marketing for African businesses",
}
STORYBOARD = {
    "total_duration_seconds": 10,
    "creative_direction": "A busy owner is overwhelmed, then Jane takes over their posting.",
    "scenes": [
        {"scene_number": 1, "video_prompt": "A tired owner at a desk", "text_overlay": "Before", "dialogue": None},
        {"scene_number": 2, "video_prompt": "Jane beside the owner", "text_overlay": "Let Jane handle it", "dialogue": "Jane has got this"},
    ],
}


def _bc():
    return BrandProfileService.to_brand_context(dict(PROFILE))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── the prompt ────────────────────────────────────────────────────────────────

def test_the_prompt_carries_what_the_video_is_about():
    prompt = vc.build_prompt(STORYBOARD, "instagram", _bc())
    assert "A busy owner is overwhelmed, then Jane takes over their posting." in prompt
    assert 'on-screen text: "Let Jane handle it"' in prompt and 'spoken: "Jane has got this"' in prompt
    assert "A tired owner at a desk" in prompt


def test_the_prompt_carries_the_playbook():
    prompt = vc.build_prompt(STORYBOARD, "instagram", _bc())
    assert "NEVER use these words or phrases: cheap" in prompt
    assert "lack of time" in prompt and "AI sales and marketing for African businesses" in prompt
    assert "Brand voice/tone: warm" in prompt


def test_the_prompt_forbids_generic_captions():
    assert "Never a generic line" in vc.build_prompt(STORYBOARD, "tiktok", _bc())


def test_with_no_playbook_and_an_old_style_script_it_still_works():
    prompt = vc.build_prompt({"scenes": [{"scene_description": "A bakery counter"}]}, "tiktok", {})
    assert "A bakery counter" in prompt and "TikTok" in prompt


def test_an_unknown_platform_falls_back_to_instagram():
    assert "INSTAGRAM" in vc.build_prompt(STORYBOARD, "myspace", _bc())


# ── finishing ─────────────────────────────────────────────────────────────────

def test_banned_words_and_emojis_are_removed():
    out = vc.finish("Cheap, quick wins 😀 for busy owners.\n\n#a #b", _bc())
    assert "cheap" not in out.lower() and "😀" not in out


def test_the_call_to_action_closes_the_text_above_the_hashtags():
    out = vc.finish("Jane takes over your posting.\n\n#growth #africa", _bc())
    lines = out.split("\n")
    assert lines[-1] == "#growth #africa"
    assert "Try Uri Social today" in lines[-3] or "Try Uri Social today" in lines[-2]


def test_hashtags_are_limited_to_what_the_playbook_allows():
    out = vc.finish("Jane helps.\n\n#a #b #c #d", _bc())
    assert out.split("\n")[-1] == "#a #b"


def test_an_empty_caption_stays_empty():
    assert vc.finish("   ", _bc()) == ""


def test_a_caption_that_already_has_the_cta_does_not_get_a_second_one():
    out = vc.finish("Jane helps. Try Uri Social today\n\n#a", _bc())
    assert out.lower().count("try uri social today") == 1


# ── the route ─────────────────────────────────────────────────────────────────

CTX = {"user_id": "u1", "brand_id": "b1", "agency_id": None}


@pytest.fixture
def gemini(monkeypatch):
    seen = {"prompts": []}
    types_mod = pytypes.ModuleType("google.genai.types")
    types_mod.GenerateContentConfig = lambda **kw: kw
    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.types = types_mod
    google_mod = pytypes.ModuleType("google")
    google_mod.genai = genai_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)

    class Models:
        def generate_content(self, model, contents, config):
            seen["prompts"].append(contents)
            return SimpleNamespace(text=seen["reply"])

    seen["reply"] = "Cheap wins for busy owners 😀\n\n#a #b #c"
    monkeypatch.setattr(vss, "_gemini_client", SimpleNamespace(models=Models()), raising=False)

    async def fake_get(user_id, db, brand_id=None):
        seen["brand"] = (user_id, brand_id)
        return {"status": True, "responseData": dict(PROFILE)}

    monkeypatch.setattr(BrandProfileService, "get", staticmethod(fake_get))
    return seen


def test_the_route_writes_from_the_script_and_the_active_brand_and_applies_the_rules(gemini):
    out = _run(R.generate_video_caption(R.GenerateVideoCaptionRequest(storyboard=STORYBOARD, platform="instagram"), None, CTX))
    assert gemini["brand"] == ("u1", "b1")
    assert "A busy owner is overwhelmed" in gemini["prompts"][0] and "NEVER use these words" in gemini["prompts"][0]
    caption = out["responseData"]["caption"]
    assert "cheap" not in caption.lower() and "😀" not in caption
    assert caption.split("\n")[-1] == "#a #b" and "Try Uri Social today" in caption


def test_the_route_says_so_when_captions_are_not_configured(monkeypatch, gemini):
    monkeypatch.setattr(vss, "_gemini_client", None, raising=False)
    with pytest.raises(HTTPException) as e:
        _run(R.generate_video_caption(R.GenerateVideoCaptionRequest(storyboard=STORYBOARD), None, CTX))
    assert e.value.status_code == 503
