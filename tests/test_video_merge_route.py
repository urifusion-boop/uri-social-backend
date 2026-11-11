"""
The merge route: it honours the owner switching their logo off, and always fails as a proper error response (an
exception that escapes becomes a bare 500 outside the CORS layer, which the browser shows as a "CORS error" with no
reason, however the server really failed).
"""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents.social_media_manager.routers import complete_social_manager as R
from app.agents.social_media_manager.services import video_generation_service as vgs
from app.agents.social_media_manager.services import video_merge_service as vms

TOKEN = {"claims": {"userId": "u1"}}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def route(monkeypatch):
    seen = SimpleNamespace(context_reads=0, logo="unset")

    async def fake_get_job(job_id):
        return {"status": "complete", "clips": [{"scene_number": 1, "video_url": "u1", "text_overlay": None}]}

    async def fake_context(ctx, db):
        seen.context_reads += 1
        return {"logo_url": "https://cdn/logo.png", "logo_position": "top_left", "logo_size": "small"}

    monkeypatch.setattr(vgs, "get_job", fake_get_job)
    monkeypatch.setattr(R, "_video_brand_context", fake_context)

    def merge_with(fn):
        async def merge(urls, texts=None, logo=None):
            seen.logo = logo
            return await fn()
        monkeypatch.setattr(vms.VideoMergeService, "merge_clips", staticmethod(merge))
    seen.merge_with = merge_with
    return seen


def _call(request=None):
    return _run(R.merge_video_job("J1", TOKEN, None, {"user_id": "u1", "brand_id": "b1"}, request))


async def _ok():
    return "https://cdn/merged.mp4"


def test_the_logo_is_on_unless_the_owner_switched_it_off(route):
    route.merge_with(_ok)
    _call(None)
    assert route.logo == {"url": "https://cdn/logo.png", "position": "top_left", "size": "small"}
    _call(R.MergeVideoRequest(use_logo=True))
    assert route.logo is not None


def test_switched_off_means_no_logo_and_the_playbook_is_not_even_read(route):
    route.merge_with(_ok)
    _call(R.MergeVideoRequest(use_logo=False))
    assert route.logo is None and route.context_reads == 0


@pytest.mark.parametrize("failure, status", [
    (vms.VideoMergeError("clip 2 could not be downloaded (ConnectionError)"), 502),
    (subprocess.CalledProcessError(1, "ffmpeg", stderr=b"Invalid data"), 500),
    (subprocess.CalledProcessError(1, "ffmpeg", stderr=None), 500),
    (RuntimeError("upload blew up"), 500),
    (FileNotFoundError("ffmpeg"), 500),
])
def test_every_failure_leaves_as_a_proper_error_response_never_an_escaped_exception(route, failure, status):
    async def boom():
        raise failure
    route.merge_with(boom)
    with pytest.raises(HTTPException) as e:
        _call(None)
    assert e.value.status_code == status
    assert "Please try again" in e.value.detail
    assert "Traceback" not in e.value.detail and "ffmpeg" not in e.value.detail.lower().replace("video tool", "")
