"""
The photos the customer uploads are used by every storyboard frame, not just scene 1.

Frames were drawn from the previous frame only, with the owner's first photo used just for scene 1 (and for the face
of a person). By scene 2 the product had drifted from the real photo, and photos 2 to 5 were never used. These tests
pin that each frame is drawn with the owner's photos in view.
"""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.services import video_creative_service as vcs


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Images:
    def __init__(self, reject_lists=False):
        self.calls, self.reject_lists = [], reject_lists

    def edit(self, **kw):
        self.calls.append(kw)
        if self.reject_lists and isinstance(kw["image"], list):
            raise RuntimeError("multi-image edit rejected")
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"img").decode())])

    generate = edit


@pytest.fixture
def frames(monkeypatch):
    def make(reject_lists=False):
        images = _Images(reject_lists)
        monkeypatch.setattr(vcs, "openai_client", SimpleNamespace(images=images))

        async def fake_upload(data, folder="", resource_type=""):
            return "https://cdn.test/new.png"

        async def fake_download(url):
            return b"bytes-of:" + url.encode()

        monkeypatch.setattr(vcs, "upload_bytes", fake_upload)
        monkeypatch.setattr(vcs.VideoCreativeService, "_download_image_bytes", staticmethod(fake_download))
        monkeypatch.setattr(vcs.VideoCreativeService, "_as_file", staticmethod(lambda b: b))
        monkeypatch.setattr(vcs.VideoStoryboardService, "_decode_brand_image",
                            staticmethod(lambda ref: b"photo:" + ref.encode()))
        return images
    return make


PRODUCT = {"scene_number": 2, "shot_type": "product_hero", "motion": "slow push in",
           "video_prompt": "The can sits on a marble counter, juice splashing"}
FIRST = {**PRODUCT, "scene_number": 1}


def _draw(scene, refs, prev=None, identity=None):
    return _run(vcs.VideoCreativeService._generate_scene_frame(scene, refs, prev, None, identity))


def test_scene_one_is_drawn_from_all_the_photos(frames):
    images = frames()
    _draw(FIRST, ["a", "b"])
    assert images.calls[0]["image"] == [b"photo:a", b"photo:b"]
    assert "exactly as shown" in images.calls[0]["prompt"]


def test_a_single_photo_is_passed_as_one_image(frames):
    images = frames()
    _draw(FIRST, ["a"])
    assert images.calls[0]["image"] == b"photo:a"


def test_a_later_scene_without_people_still_sees_the_photos(frames):
    images = frames()
    _draw(PRODUCT, ["a", "b"], prev="https://cdn.test/scene1.png")
    call = images.calls[0]
    assert call["image"] == [b"bytes-of:https://cdn.test/scene1.png", b"photo:a", b"photo:b"]
    assert "Image 1 is the previous moment" in call["prompt"]
    assert vcs.VideoCreativeService._REFERENCE_FIDELITY in call["prompt"]
    assert vcs.IDENTITY_LOCK not in call["prompt"]            # no person in this frame


def test_a_later_scene_with_a_person_sees_the_first_appearance_then_the_photos(frames):
    images = frames()
    scene = {**PRODUCT, "video_prompt": "A woman holds the can and smiles"}
    _draw(scene, ["a"], prev="https://cdn.test/scene2.png", identity="https://cdn.test/scene1.png")
    call = images.calls[0]
    assert call["image"] == [b"bytes-of:https://cdn.test/scene2.png", b"bytes-of:https://cdn.test/scene1.png", b"photo:a"]
    assert vcs.IDENTITY_LOCK in call["prompt"]


def test_no_photos_means_the_frame_is_drawn_as_before(frames):
    images = frames()
    _draw(PRODUCT, [], prev="https://cdn.test/scene1.png")
    assert images.calls[0]["image"] == b"bytes-of:https://cdn.test/scene1.png"
    assert images.calls[0]["prompt"].startswith("Keep the same subject")


def test_at_most_three_photos_go_to_each_frame(frames):
    images = frames()
    _draw(FIRST, ["a", "b", "c", "d", "e"])
    assert images.calls[0]["image"] == [b"photo:a", b"photo:b", b"photo:c"]


def test_a_photo_that_cannot_be_read_is_skipped(frames, monkeypatch):
    images = frames()

    def decode(ref):
        if ref == "bad":
            raise ValueError("not base64")
        return b"photo:" + ref.encode()

    monkeypatch.setattr(vcs.VideoStoryboardService, "_decode_brand_image", staticmethod(decode))
    _draw(FIRST, ["bad", "a", "b"])
    assert images.calls[0]["image"] == [b"photo:a", b"photo:b"]


def test_if_several_photos_are_rejected_scene_one_falls_back_to_the_first(frames):
    images = frames(reject_lists=True)
    assert _draw(FIRST, ["a", "b"]) == "https://cdn.test/new.png"
    assert images.calls[1]["image"] == b"photo:a"


def test_if_the_photos_are_rejected_a_later_scene_is_drawn_from_the_previous_frame(frames):
    images = frames(reject_lists=True)
    assert _draw(PRODUCT, ["a"], prev="https://cdn.test/scene1.png") == "https://cdn.test/new.png"
    assert images.calls[1]["image"] == b"bytes-of:https://cdn.test/scene1.png"
    assert images.calls[1]["prompt"].startswith("Keep the same subject")
