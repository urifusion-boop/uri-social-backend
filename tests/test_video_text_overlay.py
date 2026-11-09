"""
On-screen text in generated videos is drawn sharply by ffmpeg when the clips are joined, not baked into the
picture by the image model (where the clip model then smeared it).
"""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.agents.social_media_manager.routers import complete_social_manager as R
from app.agents.social_media_manager.services import video_creative_service as vcs
from app.agents.social_media_manager.services import video_generation_service as vgs
from app.agents.social_media_manager.services import video_merge_service as vms
from app.agents.social_media_manager.services import video_storyboard_service as vss
from app.agents.social_media_manager.services import video_text_overlay as ov

TOKEN = {"claims": {"userId": "u1"}}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── the text itself ───────────────────────────────────────────────────────────

def test_emoji_and_stray_whitespace_are_removed_because_the_font_cannot_draw_them():
    assert ov.clean_overlay_text("  Fresh   jollof 🔥 today\n ✨ ") == "Fresh jollof today"
    assert ov.clean_overlay_text(None) == ""
    assert ov.clean_overlay_text("Ẹ kú àbọ̀ ṣe") == "Ẹ kú àbọ̀ ṣe"          # Yoruba letters survive


def test_short_text_is_one_big_line():
    text, size = ov.wrap_overlay_text("Order today", 1080)
    assert text == "Order today" and size == ov._FONT_SIZE


def test_long_text_wraps_inside_the_video_width():
    text, size = ov.wrap_overlay_text("Hot jollof rice delivered fresh to your door every single morning", 1080)
    lines = text.split("\n")
    assert 1 < len(lines) <= ov._MAX_LINES
    usable = 1080 - 2 * ov._SIDE_SAFE
    assert all(len(line) * size * 0.66 <= usable + size for line in lines)
    assert ov._MIN_FONT_SIZE <= size <= ov._FONT_SIZE


def test_a_very_long_caption_shrinks_then_stops_at_four_lines():
    text, size = ov.wrap_overlay_text("word " * 60, 1080)
    assert len(text.split("\n")) <= ov._MAX_LINES and size >= ov._MIN_FONT_SIZE


def test_nothing_to_draw_gives_no_text():
    assert ov.wrap_overlay_text("   🔥  ", 1080)[0] == ""


def test_the_filter_reads_text_from_a_file_has_no_command_expansion_and_sits_clear_of_the_bottom():
    f = ov.drawtext_filter("/t/text_0.txt", 66, 5.0)
    assert "textfile=/t/text_0.txt" in f and "expansion=none" in f
    assert "fontfile=" + ov.FONT_FILE in f
    assert "fontcolor=white" in f and "borderw=4" in f and "box=1" in f      # outline + soft box for contrast
    assert "y=h-text_h-h*0.2" in f
    assert "between(t,0.25,4.80)" in f
    assert "enable" not in ov.drawtext_filter("/t/x.txt", 66, None)          # unknown length: shown throughout


# ── the merge draws it after scaling, and never loses a clip over it ─────────

@pytest.fixture
def ffmpeg(monkeypatch):
    log = SimpleNamespace(cmds=[], fail_on_drawtext=False)

    def fake_run(cmd, **kw):
        log.cmds.append(cmd)
        if cmd[0] == "ffprobe":
            out = "5.0" if "format=duration" in cmd else '{"streams": [{"codec_type": "audio"}]}'
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        vf = cmd[cmd.index("-vf") + 1] if "-vf" in cmd else ""
        if log.fail_on_drawtext and "drawtext" in vf:
            raise subprocess.CalledProcessError(1, cmd, stderr=b"Could not load font")
        with open(cmd[-1], "wb") as f:
            f.write(b"video")
        return SimpleNamespace(stdout="", stderr=b"", returncode=0)

    monkeypatch.setattr(vms.subprocess, "run", fake_run)
    return log


def _vfs(log):
    return [c[c.index("-vf") + 1] for c in log.cmds if c[0] == "ffmpeg" and "-vf" in c]


def test_a_clip_with_text_gets_it_drawn_after_the_scale_to_final_size(ffmpeg):
    vms.VideoMergeService._normalize_and_concat([b"a", b"b"], ["Order today", None])
    vfs = _vfs(ffmpeg)
    assert len(vfs) == 2
    assert "drawtext" in vfs[0] and vfs[0].index("scale=1080:1920") < vfs[0].index("drawtext")
    assert "drawtext" not in vfs[1]


def test_no_overlay_list_merges_exactly_as_before(ffmpeg):
    vms.VideoMergeService._normalize_and_concat([b"a", b"b"])
    assert all("drawtext" not in vf for vf in _vfs(ffmpeg))


def test_if_the_text_cannot_be_drawn_the_clip_is_still_merged_without_it(ffmpeg):
    ffmpeg.fail_on_drawtext = True
    out = vms.VideoMergeService._normalize_and_concat([b"a"], ["Order today"])
    assert out == b"video"
    vfs = _vfs(ffmpeg)
    assert "drawtext" in vfs[0] and "drawtext" not in vfs[1]            # tried with text, then without


# ── the route hands each clip's own text to the merge ────────────────────────

def test_the_merge_route_passes_each_finished_clips_text(monkeypatch):
    seen = {}

    async def fake_get_job(job_id):
        return {"status": "complete", "clips": [
            {"scene_number": 1, "video_url": "u1", "text_overlay": "Fresh today"},
            {"scene_number": 2, "video_url": None, "text_overlay": "failed scene"},
            {"scene_number": 3, "video_url": "u3", "text_overlay": None},
        ]}

    async def fake_merge(urls, texts=None):
        seen["args"] = (urls, texts)
        return "https://cdn/merged.mp4"

    monkeypatch.setattr(vgs, "get_job", fake_get_job)
    monkeypatch.setattr(vms.VideoMergeService, "merge_clips", staticmethod(fake_merge))
    _run(R.merge_video_job("J1", TOKEN))
    assert seen["args"] == (["u1", "u3"], ["Fresh today", None])         # the failed scene's text is dropped with it


# ── the pictures no longer carry the text ────────────────────────────────────

def test_script_writers_are_told_the_editor_draws_the_text():
    for prompt in (vcs._SYSTEM_PROMPT, vss._SYSTEM_PROMPT):
        assert "8 words or fewer" in prompt and "NEVER describe lettering" in prompt


def test_the_frame_prompt_no_longer_asks_the_image_model_to_draw_the_text(monkeypatch):
    prompts = []

    class Images:
        def generate(self, **kw):
            prompts.append(kw["prompt"])
            import base64
            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"i").decode())])

    async def fake_upload(data, folder="", resource_type=""):
        return "https://cdn/f.png"

    monkeypatch.setattr(vcs, "openai_client", SimpleNamespace(images=Images()))
    monkeypatch.setattr(vcs, "upload_bytes", fake_upload)
    scene = {"scene_number": 1, "shot_type": "lifestyle", "video_prompt": "Rice steams", "motion": "push in",
             "text_overlay": "Order today"}
    _run(vcs.VideoCreativeService._generate_scene_frame(scene, [], None, None))
    p = prompts[0]
    assert 'On-screen text: "Order today"' not in p and '"Order today"' not in p
    assert "draw NO text" in p and "lower third" in p
