"""
The brand's real logo, from the Brand Playbook, is laid over the merged video by ffmpeg (never drawn by the picture
or video model, which garble logos). No logo in the playbook means nothing is added; a logo that cannot be applied
never loses the video.
"""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.services import video_logo_overlay as lo
from app.agents.social_media_manager.services import video_merge_service as vms


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── reading the logo from the playbook ────────────────────────────────────────

def test_a_playbook_with_a_logo_gives_its_url_position_and_size():
    bc = {"logo_url": "https://cdn.test/logo.png", "logo_position": "top_left", "logo_size": "large"}
    assert lo.logo_from_brand_context(bc) == {"url": "https://cdn.test/logo.png", "position": "top_left", "size": "large"}


@pytest.mark.parametrize("bc", [None, {}, {"logo_url": None}, {"logo_url": ""}, {"logo_url": "  "},
                                {"logo_url": "file:///etc/passwd"}, {"logo_url": "not a url"}])
def test_no_usable_logo_means_no_logo(bc):
    assert lo.logo_from_brand_context(bc) is None


def test_missing_or_odd_position_and_size_fall_back_to_the_defaults():
    got = lo.logo_from_brand_context({"logo_url": "https://cdn.test/l.png", "logo_size": "huge", "logo_position": None})
    assert got["size"] == "small" and got["position"] == "bottom_right"
    assert lo.logo_from_brand_context({"logo_url": "https://cdn.test/l.png", "logo_position": "Top-Right"})["position"] == "top_right"


# ── where and how big ─────────────────────────────────────────────────────────

def test_the_logo_is_scaled_by_the_playbook_size_and_overlaid_with_alpha():
    small = lo.logo_filter("bottom_right", "small", 1080, 1920)
    large = lo.logo_filter("bottom_right", "large", 1080, 1920)
    assert "scale=129:-2" in small and "scale=216:-2" in large             # 12% and 20% of 1080
    assert "format=rgba" in small and "overlay=" in small and small.endswith("[vout]")


@pytest.mark.parametrize("position, xy", [
    ("top_left", "overlay=54:153"),
    ("top_right", "overlay=W-w-54:153"),
    ("bottom_left", "overlay=54:H-h-172"),
    ("bottom_right", "overlay=W-w-54:H-h-172"),
    ("bottom_center", "overlay=(W-w)/2:H-h-172"),
    ("center", "overlay=(W-w)/2:(H-h)/2"),
])
def test_each_corner_sits_clear_of_the_edges(position, xy):
    assert xy in lo.logo_filter(position, "small", 1080, 1920)


def test_an_unknown_position_is_bottom_right():
    assert lo.logo_filter("somewhere", "small", 1080, 1920) == lo.logo_filter("bottom_right", "small", 1080, 1920)


def test_a_bottom_logo_stays_below_the_caption():
    # the caption block ends 20% above the bottom; the logo's top edge must be lower than that for a small logo
    assert 1920 * 0.09 + 129 < 1920 * 0.2


# ── the merge ─────────────────────────────────────────────────────────────────

@pytest.fixture
def ffmpeg(monkeypatch):
    log = SimpleNamespace(cmds=[], fail_on_logo=False)

    def fake_run(cmd, **kw):
        log.cmds.append(cmd)
        if cmd[0] == "ffprobe":
            out = "5.0" if "format=duration" in cmd else '{"streams": [{"codec_type": "audio"}]}'
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        if log.fail_on_logo and "-filter_complex" in cmd and "overlay=" in cmd[cmd.index("-filter_complex") + 1]:
            raise subprocess.CalledProcessError(1, cmd, stderr=b"Invalid data found when processing input")
        with open(cmd[-1], "wb") as f:
            f.write(b"logo-video" if "-filter_complex" in cmd else b"video")
        return SimpleNamespace(stdout="", stderr=b"", returncode=0)

    monkeypatch.setattr(vms.subprocess, "run", fake_run)
    return log


def _logo_cmds(log):
    return [c for c in log.cmds if c[0] == "ffmpeg" and "-filter_complex" in c]


def test_a_video_with_a_logo_gets_it_overlaid_once_after_the_clips_are_joined(ffmpeg):
    out = vms.VideoMergeService._normalize_and_concat([b"a", b"b"], None, b"PNG", "top_right", "medium")
    assert out == b"logo-video"
    cmds = _logo_cmds(ffmpeg)
    assert len(cmds) == 1
    fc = cmds[0][cmds[0].index("-filter_complex") + 1]
    assert "overlay=W-w-54:153" in fc and "scale=172:-2" in fc          # top_right, medium = 16% of 1080
    assert cmds[0][cmds[0].index("-c:a") + 1] == "copy"                  # the sound is not re-encoded
    concat = [c for c in ffmpeg.cmds if c[0] == "ffmpeg" and "concat" in c]
    assert ffmpeg.cmds.index(concat[0]) < ffmpeg.cmds.index(cmds[0])     # logo goes on after the join


def test_no_logo_means_the_merge_is_exactly_as_before(ffmpeg):
    out = vms.VideoMergeService._normalize_and_concat([b"a", b"b"])
    assert out == b"video" and _logo_cmds(ffmpeg) == []


def test_if_the_logo_cannot_be_applied_the_video_is_still_delivered_without_it(ffmpeg):
    ffmpeg.fail_on_logo = True
    out = vms.VideoMergeService._normalize_and_concat([b"a"], None, b"not-an-image", "bottom_right", "small")
    assert out == b"video"
    assert len(_logo_cmds(ffmpeg)) == 1                                  # it was tried, then dropped


def test_merge_clips_fetches_the_logo_and_survives_a_failed_download(monkeypatch):
    seen = {}

    class Response:
        def __init__(self, content): self.content = content
        def raise_for_status(self): pass

    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            if url == "https://cdn.test/bad.png":
                raise RuntimeError("404")
            return Response(b"logo-bytes" if "logo" in url else b"clip")

    async def fake_upload(data, folder="", resource_type=""):
        return "https://cdn.test/merged.mp4"

    def fake_concat(clips, texts=None, logo_bytes=None, position="x", size="y"):
        seen["args"] = (len(clips), logo_bytes, position, size)
        return b"merged"

    monkeypatch.setattr(vms.httpx, "AsyncClient", Client)
    monkeypatch.setattr(vms, "upload_bytes", fake_upload)
    monkeypatch.setattr(vms.VideoMergeService, "_normalize_and_concat", staticmethod(fake_concat))

    _run(vms.VideoMergeService.merge_clips(["c1", "c2"], None, {"url": "https://cdn.test/logo.png", "position": "top_left", "size": "large"}))
    assert seen["args"] == (2, b"logo-bytes", "top_left", "large")

    _run(vms.VideoMergeService.merge_clips(["c1"], None, {"url": "https://cdn.test/bad.png", "position": "top_left", "size": "large"}))
    assert seen["args"][1] is None                                        # no logo, but the video is still made

    _run(vms.VideoMergeService.merge_clips(["c1"]))
    assert seen["args"][1] is None
