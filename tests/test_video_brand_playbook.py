"""
Video scripts follow the Brand Playbook of the brand the user is working in.

The storyboard writers already asked the Brand Brief for rules, voice, audience, offer and promotions, but the
routes handed them a five-key dict (name, industry, colours, voice, region), so those sections were always
empty. These tests pin the whole chain: the routes load the ACTIVE brand's full Playbook, the prompt carries it,
and what viewers read or hear can't break the word rules whatever the model wrote.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app.agents.social_media_manager.routers import complete_social_manager as R
from app.agents.social_media_manager.services import brand_brief as bb
from app.agents.social_media_manager.services import video_creative_service as vcs
from app.agents.social_media_manager.services import video_storyboard_service as vss
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

PROFILE = {
    "brand_name": "Bukka Express",
    "industry": "food",
    "region": "Lagos",
    "brand_colors": ["#FF6A00", "#1B1B1B"],
    "derived_voice": "warm and witty",
    "voice_sample": "Hot jollof. No stories.",
    "guardrails": {"avoid_topics": "politics", "banned_words": "cheap", "emoji_usage": "no"},
    "words_to_avoid": ["deal of the century"],
    "languages": ["English", "Pidgin"],
    "audience_age_range": "Gen Z (18-24)",
    "product_description": "Home-style Nigerian food delivered",
    "unique_selling_proposition": "cooked fresh every morning",
    "business_pulse": {"current_promotions": ["Free drink with every combo this week"]},
    "business_pulse_updated_at": None,
    "playbook_instructions": [
        {"text": "Always say 'order on WhatsApp' at the end", "applies_to": ["video"], "platforms": []},
        {"text": "Open captions with a hook", "applies_to": ["posts"], "platforms": []},
    ],
}
CTX = {"user_id": "u1", "brand_id": "b-active", "agency_id": None}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _bc():
    return BrandProfileService.to_brand_context(dict(PROFILE))


# ── the routes load the active brand's whole Playbook ─────────────────────────

@pytest.fixture
def routes(monkeypatch):
    seen = {"get_calls": [], "jobs": []}

    async def fake_get(user_id, db, brand_id=None):
        seen["get_calls"].append((user_id, brand_id))
        return {"status": True, "responseData": dict(PROFILE)}

    async def fake_create_job(job_id=None):
        return job_id or "job1"

    async def fake_run(*args):
        seen["jobs"].append(args)

    monkeypatch.setattr(BrandProfileService, "get", staticmethod(fake_get))
    monkeypatch.setattr(vcs.VideoCreativeService, "create_storyboard_job", staticmethod(fake_create_job))
    monkeypatch.setattr(vcs.VideoCreativeService, "run_storyboard_job", staticmethod(fake_run))
    monkeypatch.setattr(vss.VideoStoryboardService, "create_storyboard_job", staticmethod(fake_create_job))
    monkeypatch.setattr(vss.VideoStoryboardService, "run_storyboard_job", staticmethod(fake_run))
    return seen


def _brand_context_given_to_job(tasks):
    task = tasks.tasks[0]
    return task.args[3]


def test_describe_it_storyboard_gets_the_full_playbook_of_the_active_brand(routes):
    tasks = BackgroundTasks()
    request = R.CreativeStoryboardRequest(brief="A fun ad for our lunch combo this week")
    _run(R.generate_creative_storyboard(request, tasks, None, CTX))
    assert routes["get_calls"] == [("u1", "b-active")]     # the brand the user switched to, not just their own
    bc = _brand_context_given_to_job(tasks)
    assert bc["guardrails"]["avoid_topics"] == "politics"
    assert bc["words_to_avoid"] == ["deal of the century"]
    assert bc["languages"] == ["English", "Pidgin"]
    assert bc["business_pulse"]["current_promotions"] == ["Free drink with every combo this week"]
    assert bc["playbook_instructions"][0]["applies_to"] == ["video"]
    assert bc["brand_voice"] == "warm and witty"


def test_upload_storyboard_gets_the_same_playbook(routes):
    tasks = BackgroundTasks()
    request = R.StoryboardRequest(brand_images=["data:image/png;base64,AAAA"])
    _run(R.generate_storyboard(request, tasks, None, CTX))
    assert routes["get_calls"] == [("u1", "b-active")]
    bc = _brand_context_given_to_job(tasks)
    assert bc["guardrails"]["banned_words"] == "cheap" and bc["unique_selling_proposition"] == "cooked fresh every morning"


def test_a_brand_with_an_empty_playbook_still_generates(monkeypatch, routes):
    async def empty(user_id, db, brand_id=None):
        return {"status": False}
    monkeypatch.setattr(BrandProfileService, "get", staticmethod(empty))
    tasks = BackgroundTasks()
    _run(R.generate_creative_storyboard(R.CreativeStoryboardRequest(brief="A fun ad for our lunch combo"), tasks, None, CTX))
    assert _brand_context_given_to_job(tasks) == {}


# ── the Describe It prompt carries it ─────────────────────────────────────────

SCRIPT = {
    "creative_direction": "A cheap, seamless lunch story 😀",
    "video_style": "clean_commercial",
    "scenes": [
        {"scene_number": 1, "duration_seconds": 5, "text_overlay": "Cheap jollof today 😀", "dialogue": "Come and eat cheap food",
         "video_prompt": "A steaming plate of jollof rice on a cheap plastic table"},
        {"scene_number": 2, "duration_seconds": 5, "text_overlay": None, "dialogue": None, "video_prompt": "Hands lifting a lid"},
    ],
}


class _FakeOpenAI:
    def __init__(self, script):
        self.script, self.calls = script, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.script)))])


def _describe_it(monkeypatch, bc, script=SCRIPT, **kw):
    fake = _FakeOpenAI(json.loads(json.dumps(script)))
    monkeypatch.setattr(vcs, "openai_client", fake)
    result = _run(vcs.VideoCreativeService.generate_creative_storyboard(
        brief="A fun ad for our lunch combo", reference_images=[], brand_context=bc, **kw))
    return result, fake


def _user_prompt(fake):
    return fake.calls[0]["messages"][1]["content"][0]["text"]


def test_the_describe_it_prompt_carries_rules_voice_audience_offer_and_promotions(monkeypatch):
    _, fake = _describe_it(monkeypatch, _bc(), target_platform="instagram_reels")
    prompt = _user_prompt(fake)
    assert "NEVER mention or reference: politics." in prompt
    assert "deal of the century" in prompt and "cheap" in prompt           # both banned-word lists
    assert "Pidgin" in prompt                                              # the language rule
    assert "Use NO emojis" in prompt
    assert "order on WhatsApp" in prompt                                   # the owner's own video instruction
    assert "Open captions with a hook" not in prompt                       # ...but not one meant for posts
    assert "Gen Z" in prompt                                               # audience
    assert "cooked fresh every morning" in prompt                          # offer
    assert "Free drink with every combo this week" in prompt               # what's on right now
    assert "Hot jollof. No stories." in prompt                             # how the brand really writes


def test_an_owner_instruction_for_another_platform_is_left_out(monkeypatch):
    bc = _bc()
    bc["playbook_instructions"] = [
        {"text": "Keep it formal", "applies_to": ["video"], "platforms": ["linkedin"]},
        {"text": "Use trending sounds language", "applies_to": ["video"], "platforms": ["tiktok"]},
    ]
    _, fake = _describe_it(monkeypatch, bc, target_platform="tiktok")
    prompt = _user_prompt(fake)
    assert "trending sounds" in prompt and "Keep it formal" not in prompt


def test_an_empty_playbook_changes_nothing_about_the_prompt(monkeypatch):
    result, fake = _describe_it(monkeypatch, {})
    assert result["status"] is True
    assert "BRAND BRIEF" not in _user_prompt(fake)


# ── what viewers read and hear can't break the word rules ─────────────────────

def test_banned_words_and_emojis_never_reach_the_script(monkeypatch):
    result, _ = _describe_it(monkeypatch, _bc())
    board = result["storyboard"]
    scene = board["scenes"][0]
    for text in (scene["text_overlay"], scene["dialogue"], board["creative_direction"]):
        assert "cheap" not in text.lower() and "😀" not in text
    # untouched where there was nothing to fix
    assert board["scenes"][1]["text_overlay"] is None and board["scenes"][1]["dialogue"] is None
    # video_prompt describes pictures, not words: not rewritten
    assert scene["video_prompt"] == "A steaming plate of jollof rice on a cheap plastic table"


def test_the_owners_own_edited_direction_is_never_rewritten(monkeypatch):
    mine = "A cheap and cheerful story, exactly as I wrote it"
    result, _ = _describe_it(monkeypatch, _bc(), creative_direction_override=mine)
    assert result["storyboard"]["creative_direction"] == mine


def test_the_floor_is_the_same_for_the_upload_flow():
    board = json.loads(json.dumps(SCRIPT))
    bb.enforce_video_script(board, _bc())
    assert "cheap" not in board["scenes"][0]["dialogue"].lower()
    assert "😀" not in board["creative_direction"]


def test_the_floor_does_nothing_without_a_playbook():
    board = json.loads(json.dumps(SCRIPT))
    assert bb.enforce_video_script(board, {}) == SCRIPT
    assert bb.enforce_video_script(None, _bc()) is None


@pytest.mark.parametrize("target,expected", [
    ("instagram_reels", "instagram"), ("tiktok", "tiktok"), ("youtube_shorts", "youtube"),
    ("facebook_reels", "facebook"), ("", ""), (None, ""),
])
def test_video_target_to_social_platform(target, expected):
    assert bb.video_platform(target) == expected


# ── the upload (Gemini) flow gets the same prompt ─────────────────────────────

def test_upload_storyboard_prompt_and_floor(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            captured["text"] = contents[0].text if hasattr(contents[0], "text") else str(contents[0])
            return SimpleNamespace(text=json.dumps(json.loads(json.dumps(SCRIPT))))

    class _Part:
        def __init__(self, text=None):
            self.text = text

        @staticmethod
        def from_text(text):
            return _Part(text)

        @staticmethod
        def from_bytes(data, mime_type):
            return _Part()

    fake_types = SimpleNamespace(Part=_Part, GenerateContentConfig=lambda **kw: kw)
    monkeypatch.setattr(vss, "genai_types", fake_types, raising=False)   # absent when google-genai isn't installed
    monkeypatch.setattr(vss, "_gemini_client", SimpleNamespace(models=FakeModels()))
    image = "data:image/png;base64," + base64.b64encode(b"x" * 16).decode()
    result = _run(vss.VideoStoryboardService.generate_storyboard(
        brand_images=[image], optional_text="lunch combo", brand_context=_bc(), target_platform="tiktok"))
    assert result["status"] is True
    assert "NEVER mention or reference: politics." in captured["text"]
    assert "order on WhatsApp" in captured["text"]
    assert "cheap" not in result["storyboard"]["scenes"][0]["text_overlay"].lower()


def test_video_wording_is_not_rewritten_by_the_caption_cliche_list():
    # The Before/After style itself talks about "transformation": that must reach the creative direction intact.
    board = {"creative_direction": "We tell one simple transformation story, a transformative before and after.",
             "scenes": [{"text_overlay": "Premium results", "dialogue": "Unlock your journey", "video_prompt": "x"}]}
    bb.enforce_video_script(board, _bc())
    assert board["creative_direction"] == "We tell one simple transformation story, a transformative before and after."
    assert board["scenes"][0]["text_overlay"] == "Premium results"
    assert board["scenes"][0]["dialogue"] == "Unlock your journey"
