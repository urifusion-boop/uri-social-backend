"""
The merge runs inside the web request, so it must not fail on one dropped download and must be quick.
(The route's behaviour is in test_video_merge_route.py.)
"""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.services import video_merge_service as vms

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(vms.asyncio, "sleep", instant)


class _Response:
    def __init__(self, content=b"clip", status_ok=True):
        self.content, self._ok = content, status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("404 Not Found")


class _Client:
    """Answers each url from a list of responses/exceptions, in order."""
    def __init__(self, plan):
        self.plan, self.calls = plan, []

    async def get(self, url):
        self.calls.append(url)
        step = self.plan[len(self.calls) - 1]
        if isinstance(step, Exception):
            raise step
        return step


# ── downloads ────────────────────────────────────────────────────────────────

def test_a_dropped_download_is_tried_again():
    client = _Client([ConnectionError("reset"), _Response(b"bytes")])
    assert _run(vms.VideoMergeService._fetch(client, "https://cdn/c1.mp4", "clip 1")) == b"bytes"
    assert len(client.calls) == 2


def test_an_error_page_is_not_taken_for_a_video():
    client = _Client([_Response(b"<html>", status_ok=False)] * 3)
    with pytest.raises(vms.VideoMergeError) as e:
        _run(vms.VideoMergeService._fetch(client, "https://cdn/c1.mp4", "clip 2"))
    assert "clip 2" in str(e.value) and len(client.calls) == 3


def test_an_empty_download_counts_as_failed():
    client = _Client([_Response(b"")] * 3)
    with pytest.raises(vms.VideoMergeError):
        _run(vms.VideoMergeService._fetch(client, "u", "clip 1"))


def test_one_clip_that_cannot_be_fetched_stops_the_merge_with_a_clear_reason(monkeypatch):
    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            if url == "bad":
                raise ConnectionError("gone")
            return _Response()

    monkeypatch.setattr(vms.httpx, "AsyncClient", Client)
    with pytest.raises(vms.VideoMergeError) as e:
        _run(vms.VideoMergeService.merge_clips(["ok", "bad"]))
    assert "clip 2" in str(e.value)


# ── encoding speed ───────────────────────────────────────────────────────────

def test_clips_and_logo_are_encoded_with_the_faster_preset(monkeypatch):
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[0] == "ffprobe":
            out = "5.0" if "format=duration" in cmd else '{"streams": [{"codec_type": "audio"}]}'
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        with open(cmd[-1], "wb") as f:
            f.write(b"v")
        return SimpleNamespace(stdout="", stderr=b"", returncode=0)

    monkeypatch.setattr(vms.subprocess, "run", fake_run)
    vms.VideoMergeService._normalize_and_concat([b"a", b"b"], None, b"PNG", "top_left", "small")
    encodes = [c for c in cmds if c[0] == "ffmpeg" and "libx264" in c]
    assert len(encodes) == 3                                              # two clips + the logo pass
    assert all(c[c.index("-preset") + 1] == "veryfast" for c in encodes)
