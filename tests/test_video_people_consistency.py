"""
The same person keeps the same face across a generated video.

Each storyboard frame was drawn from the previous frame only (a copy of a copy), people were barely described, and
the clip prompt said nothing about faces — so faces drifted by scene 4 and melted in motion. These tests pin the
three fixes: the script writers' people rules, the frame step copying faces from the first frame the person
appeared in (and the owner's photo), and the clip prompt asking the video model to hold faces steady.
"""
import asyncio
import base64
import sys
import types
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.services import people_consistency as pc
from app.agents.social_media_manager.services import video_creative_service as vcs
from app.agents.social_media_manager.services import video_generation_service as V
from app.agents.social_media_manager.services import video_storyboard_service as vss
from app.core.config import settings


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── knowing when a scene has a person in it ───────────────────────────────────

@pytest.mark.parametrize("text", [
    "A young woman smiles at the camera",
    "Close-up of the chef plating jollof",
    "He opens the box and laughs",
    "Customers queue outside",
])
def test_scenes_with_people_are_recognised(text):
    assert pc.mentions_people(text)


@pytest.mark.parametrize("text", [
    "A steaming plate of jollof rice on a wooden table, slow push-in",
    "Product bottle rotating on a marble surface",
    "A 3D model of the building",
    "", None,
])
def test_scenes_without_people_are_left_alone(text):
    assert not pc.mentions_people(text)


def test_face_guidance_is_added_only_to_a_clip_that_has_people():
    assert pc.FACE_STABILITY in pc.with_face_stability("A woman laughs and waves")
    assert pc.with_face_stability("Rice steaming on a plate") == "Rice steaming on a plate"
    assert pc.with_face_stability(None) == ""


# ── the script writers are told how to write people ──────────────────────────

@pytest.mark.parametrize("prompt", [vcs._SYSTEM_PROMPT, vss._SYSTEM_PROMPT])
def test_both_script_writers_get_the_people_rules(prompt):
    assert "PEOPLE ON SCREEN" in prompt
    assert "{{PEOPLE_RULES}}" not in prompt                      # the placeholder was filled, not left in the prompt
    assert "copy that description word for word" in prompt
    assert "large, sharp and well lit" in prompt
    assert "No fast head turns" in prompt


# ── frames copy faces from the first frame the person appeared in ────────────

class _Images:
    def __init__(self, fail_on_list=False):
        self.calls, self.fail_on_list = [], fail_on_list

    def edit(self, **kw):
        self.calls.append(kw)
        if self.fail_on_list and isinstance(kw["image"], list):
            raise RuntimeError("multi-image edit rejected")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"img").decode())])

    generate = edit


@pytest.fixture
def frames(monkeypatch):
    def make(fail_on_list=False):
        images = _Images(fail_on_list)
        monkeypatch.setattr(vcs, "openai_client", SimpleNamespace(images=images))

        async def fake_upload(data, folder="", resource_type=""):
            return "https://cdn.test/new.png"

        async def fake_download(url):
            return b"bytes-of:" + url.encode()

        monkeypatch.setattr(vcs, "upload_bytes", fake_upload)
        monkeypatch.setattr(vcs.VideoCreativeService, "_download_image_bytes", staticmethod(fake_download))
        monkeypatch.setattr(vcs.VideoCreativeService, "_as_file", staticmethod(lambda b: b))
        return images
    return make


def _draw(scene, reference_images, prev, identity):
    return _run(vcs.VideoCreativeService._generate_scene_frame(scene, reference_images, prev, None, identity))


PERSON = {"scene_number": 3, "shot_type": "lifestyle", "motion": "slow push in",
          "video_prompt": "A young woman in a yellow dress smiles as she opens the box"}
NO_PERSON = {"scene_number": 3, "shot_type": "product_hero", "motion": "slow push in",
             "video_prompt": "The jollof rice steams on the plate"}


def test_a_frame_with_a_person_copies_the_face_from_the_first_frame_they_appeared_in(frames):
    images = frames()
    assert _draw(PERSON, [], "https://cdn.test/scene2.png", "https://cdn.test/scene1.png") == "https://cdn.test/new.png"
    call = images.calls[0]
    assert call["image"] == [b"bytes-of:https://cdn.test/scene2.png", b"bytes-of:https://cdn.test/scene1.png"]
    assert pc.IDENTITY_LOCK in call["prompt"]
    assert "Image 1 is the previous moment" in call["prompt"]


def test_the_owners_own_photo_is_also_an_identity_reference(frames, monkeypatch):
    images = frames()
    monkeypatch.setattr(vcs.VideoStoryboardService, "_decode_brand_image", staticmethod(lambda ref: b"owner-photo"))
    _draw(PERSON, ["data:image/png;base64,AAAA"], "https://cdn.test/scene2.png", "https://cdn.test/scene1.png")
    assert images.calls[0]["image"] == [b"bytes-of:https://cdn.test/scene2.png",
                                        b"bytes-of:https://cdn.test/scene1.png", b"owner-photo"]


def test_when_the_previous_frame_is_the_first_appearance_there_is_nothing_extra_to_copy(frames):
    images = frames()
    _draw(PERSON, [], "https://cdn.test/scene1.png", "https://cdn.test/scene1.png")
    assert images.calls[0]["image"] == b"bytes-of:https://cdn.test/scene1.png"
    assert pc.IDENTITY_LOCK not in images.calls[0]["prompt"]


def test_a_frame_without_a_person_is_drawn_exactly_as_before(frames):
    images = frames()
    _draw(NO_PERSON, [], "https://cdn.test/scene2.png", "https://cdn.test/scene1.png")
    assert images.calls[0]["image"] == b"bytes-of:https://cdn.test/scene2.png"
    assert pc.IDENTITY_LOCK not in images.calls[0]["prompt"]
    assert images.calls[0]["prompt"].startswith("Keep the same subject")


def test_if_the_extra_references_are_rejected_the_frame_is_still_drawn_from_the_previous_one(frames):
    images = frames(fail_on_list=True)
    assert _draw(PERSON, [], "https://cdn.test/scene2.png", "https://cdn.test/scene1.png") == "https://cdn.test/new.png"
    assert len(images.calls) == 2
    assert images.calls[1]["image"] == b"bytes-of:https://cdn.test/scene2.png"
    assert images.calls[1]["prompt"].startswith("Keep the same subject")


def test_the_job_remembers_the_first_frame_that_shows_a_person(monkeypatch):
    seen, pushed = [], []

    async def fake_draw(scene, refs, prev, brand, identity=None):
        seen.append((scene["scene_number"], prev, identity))
        return f"https://cdn.test/f{scene['scene_number']}.png"

    class Col:
        async def update_one(self, flt, update):
            pushed.append(update)

    monkeypatch.setattr(vcs.VideoCreativeService, "_generate_scene_frame", staticmethod(fake_draw))
    monkeypatch.setattr(vcs, "_frame_jobs_collection", lambda: Col())
    scenes = [
        {"scene_number": 1, "video_prompt": "Rice steams on a plate"},                     # no person
        {"scene_number": 2, "video_prompt": "A woman in a yellow dress takes a bite"},     # first person
        {"scene_number": 3, "video_prompt": "Close on her smile"},
        {"scene_number": 4, "video_prompt": "The box is sealed, logo on the lid"},
    ]
    _run(vcs.VideoCreativeService.run_frame_job("J1", scenes, [], None))
    assert [s[2] for s in seen] == [None, None, "https://cdn.test/f2.png", "https://cdn.test/f2.png"]
    assert [s[1] for s in seen] == [None, "https://cdn.test/f1.png", "https://cdn.test/f2.png", "https://cdn.test/f3.png"]


# ── the clip prompt asks the video model to hold faces steady ────────────────

class _Stop(Exception):
    pass


@pytest.fixture
def clip_prompt(monkeypatch):
    sent = []

    async def fake_submit(model, arguments, timeout, scene_num, job_id=None):
        sent.append(arguments)
        raise _Stop()

    monkeypatch.setitem(sys.modules, "fal_client", types.ModuleType("fal_client"))
    monkeypatch.setattr(V.VideoGenerationService, "_submit_and_poll", staticmethod(fake_submit))
    monkeypatch.setattr(settings, "FAL_API_KEY", "k", raising=False)

    def run(video_prompt, model=V._KLING_STANDARD):
        scene = {"scene_number": 1, "frame_image_url": "https://img/f.png", "duration_seconds": 5,
                 "video_prompt": video_prompt}
        with pytest.raises(_Stop):
            _run(V.VideoGenerationService._generate_scene_fal(scene, model, V.DEFAULT_AVATAR_VOICE, "job1"))
        return sent[-1]["prompt"]
    return run


def test_a_clip_with_a_person_asks_for_a_steady_face(clip_prompt):
    prompt = clip_prompt("A young woman smiles and waves at the camera")
    assert prompt.startswith("A young woman smiles and waves at the camera")
    assert pc.FACE_STABILITY in prompt


def test_a_clip_without_a_person_is_sent_unchanged(clip_prompt):
    assert clip_prompt("Rice steams on a plate, slow push-in") == "Rice steams on a plate, slow push-in"
