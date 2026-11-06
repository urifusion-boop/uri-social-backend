import asyncio
from types import SimpleNamespace

from app.agents.jane_ads import preflight


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _plan(dest="whatsapp", is_video=False, url="https://res.cloudinary.com/x/video/upload/v1/uri-ads/clip.mp4"):
    return SimpleNamespace(destination_type=dest, page_id="p1", whatsapp_number="2348012345678",
                           creative=SimpleNamespace(is_video=is_video, image_url=url))


def test_whatsapp_ad_without_a_linked_number_warns_with_the_steps(monkeypatch):
    async def none_linked(page_id):
        return False
    monkeypatch.setattr(preflight, "page_has_whatsapp", none_linked)
    out = _run(preflight.whatsapp_warning(_plan()))
    assert len(out) == 1 and "Linked accounts → WhatsApp" in out[0] and "+2348012345678" in out[0]


def test_linked_unknown_or_non_whatsapp_gives_no_warning(monkeypatch):
    for answer in (True, None):
        async def fake(page_id, a=answer):
            return a
        monkeypatch.setattr(preflight, "page_has_whatsapp", fake)
        assert _run(preflight.whatsapp_warning(_plan())) == []
    assert _run(preflight.whatsapp_warning(_plan(dest="website"))) == []


def test_video_facts_reads_a_real_video_over_http_via_ffprobe():
    # video_facts used to go through Cloudinary's admin API by public_id; it now
    # runs ffprobe directly against the URL, so this works the same for any
    # hosting provider (S3 included) instead of just Cloudinary — confirmed here
    # against a tiny real HTTP-served test file rather than a mocked API call.
    import http.server
    import socketserver
    import subprocess
    import tempfile
    import threading

    proc = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=blue:s=480x854:d=1.0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", "-f", "mp4",
         "-movflags", "frag_keyframe+empty_moov", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/clip.mp4", "wb") as f:
            f.write(proc.stdout)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=d, **kw)

            def log_message(self, *a):
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                facts = _run(preflight.video_facts(f"http://127.0.0.1:{port}/clip.mp4"))
            finally:
                httpd.shutdown()

    assert facts == {"width": 480, "height": 854, "duration": 1.0, "format": "mp4"}


def test_video_facts_returns_none_for_an_unreachable_url():
    assert _run(preflight.video_facts("http://127.0.0.1:1/missing.mp4")) is None
    assert _run(preflight.video_facts("")) is None


def test_video_problems():
    ok = {"width": 1080, "height": 1920, "duration": 20, "format": "mp4"}
    assert preflight.video_problems(ok) == []
    wide = preflight.video_problems({**ok, "width": 1920, "height": 1080})
    assert wide and "1920×1080" in wide[0]
    assert preflight.video_problems({**ok, "duration": 90})
    assert preflight.video_problems({**ok, "format": "avi"})
    assert preflight.video_problems({**ok, "width": 1080, "height": 1350}) == []


def test_bad_video_warns_and_unknown_video_does_not(monkeypatch):
    async def wide(url):
        return {"width": 1920, "height": 1080, "duration": 10, "format": "mp4"}
    monkeypatch.setattr(preflight, "video_facts", wide)
    out = _run(preflight.video_warning(_plan(is_video=True)))
    assert len(out) == 1 and "9:16" in out[0]

    async def unknown(url):
        return None
    monkeypatch.setattr(preflight, "video_facts", unknown)
    assert _run(preflight.video_warning(_plan(is_video=True))) == []
    assert _run(preflight.video_warning(_plan(is_video=False))) == []


def test_a_failing_check_never_breaks_the_plan(monkeypatch):
    async def boom(plan):
        raise RuntimeError("x")
    monkeypatch.setattr(preflight, "whatsapp_warning", boom)
    assert _run(preflight.meta_warnings(_plan(dest="website"))) == []


def test_a_tiktok_plan_says_it_runs_nationwide():
    from app.agents.jane_ads.models import GeoMode, GeoPlan
    from app.agents.jane_ads.preflight import tiktok_location_warning
    w = tiktok_location_warning(GeoPlan(mode=GeoMode.WATERING_HOLE, city="Lagos, Abuja"))
    assert "all of Nigeria" in w and "Lagos and Abuja" in w
