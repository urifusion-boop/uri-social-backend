"""
A TikTok campaign's ad copy, image brief and shoot script are written FOR TikTok — the
prompts used to say "Write a Meta/Instagram ad" whatever the campaign launched on, so a
client who picked the TikTok chip got Instagram-toned text. Meta (the default) must stay
byte-for-byte what it was.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads import creative as C
from app.core.config import settings

OK = {"headline": "h", "primary_text": "p", "image_prompt": "i",
      "video_recommended": False, "video_recommendation_reason": ""}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _prompt_of(mock):
    return mock.call_args_list[0].args[0]


def _write(platform=None):
    mock = AsyncMock(return_value=dict(OK))
    kwargs = {} if platform is None else {"platform": platform}
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock):
        _run(C.write_ad_copy("URI Social", "software", **kwargs))
    return _prompt_of(mock)


def test_a_tiktok_ad_copy_prompt_names_tiktok_not_meta_or_instagram():
    prompt = _write("tiktok")
    assert "Write a TikTok ad" in prompt
    assert "Meta" not in prompt
    assert "Instagram" not in prompt
    assert "native TikTok creator" in prompt
    assert "phone-shot" in prompt


def test_a_meta_ad_copy_prompt_is_unchanged():
    prompt = _write()
    assert "Write a Meta/Instagram ad" in prompt
    assert "TikTok" not in prompt
    assert "real Nigerian business owner texting a customer on WhatsApp" in prompt
    assert "phone-shot" not in prompt
    # Passing "meta" explicitly is the same as not passing anything.
    assert _write("meta") == prompt


def test_the_guardrails_below_the_opening_line_are_the_same_on_both_platforms():
    meta, tiktok = C._register_rules_block("whatsapp"), C._register_rules_block("whatsapp", "tiktok")
    shared = meta.split("\n", 1)[1]
    assert tiktok.split("\n", 1)[1] == shared


def test_ad_copy_written_to_match_an_image_is_also_tiktok_aware(monkeypatch):
    monkeypatch.setattr(type(settings), "jane_ads_openai_key", "k", raising=False)
    results = {}
    for platform in ("meta", "tiktok"):
        mock = AsyncMock(return_value=dict(OK))
        with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock):
            _run(C.write_ad_copy_for_image("a woman on a laptop", "URI Social", "software", platform=platform))
        results[platform] = _prompt_of(mock)
    assert "Write the copy for a TikTok ad" in results["tiktok"] and "Instagram" not in results["tiktok"]
    assert "Write the copy for a Meta/Instagram ad" in results["meta"]


def test_a_tiktok_ad_image_uses_the_tiktok_preset():
    seen = {}

    async def _fake_engine(**kw):
        seen["platform"] = kw["platform"]
        return {"image_url": "https://cdn/x.png"}

    with patch("app.agents.social_media_manager.services.image_content_service."
               "ImageContentService._generate_platform_image", new=_fake_engine):
        _run(C.generate_ad_image("content", {}, platform="tiktok"))
        assert seen["platform"] == "tiktok"
        _run(C.generate_ad_image("content", {}))
        assert seen["platform"] == "instagram"


@pytest.mark.parametrize("platform,expected,absent", [("tiktok", "TikTok ad", "Meta/Instagram"),
                                                     ("meta", "Meta/Instagram ad", "TikTok")])
def test_the_shoot_script_is_written_for_the_campaign_platform(monkeypatch, platform, expected, absent):
    monkeypatch.setattr(type(settings), "jane_ads_openai_key", "k", raising=False)
    create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    with patch("app.agents.jane_ads.creative.openai.AsyncOpenAI", return_value=client):
        _run(C.write_shoot_script("URI Social", "software", "messages", "show the product", platform=platform))

    prompt = create.call_args.kwargs["messages"][0]["content"]
    assert expected in prompt
    assert absent not in prompt
