"""
Music on Instagram Reels (Instagram Audio API). Images, carousels and Stories have no audio option in Meta's API,
so only the Reel paths carry a track: a video draft's `instagram_audio` becomes the container's
`audio_configuration`, on both the immediate and the scheduled publish path.
"""
import asyncio
import json

import pytest

from app.agents.social_media_manager.services import instagram_audio as IA
from app.agents.social_media_manager.services import instagram_direct_service as direct
from app.agents.social_media_manager.services import video_publish_service as vps


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._data


class FakeHttp:
    """Stands in for httpx.AsyncClient: records every call, answers from a queue."""
    calls: list = []
    answers: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _answer(self, method, url, params):
        FakeHttp.calls.append((method, url, dict(params or {})))
        return FakeResp(*FakeHttp.answers.pop(0)) if FakeHttp.answers else FakeResp({})

    async def get(self, url, params=None, **k):
        return await self._answer("GET", url, params)

    async def post(self, url, params=None, **k):
        return await self._answer("POST", url, params)


@pytest.fixture
def http(monkeypatch):
    FakeHttp.calls, FakeHttp.answers = [], []
    for module in (IA, direct, vps):
        monkeypatch.setattr(module.httpx, "AsyncClient", FakeHttp)
    return FakeHttp


CHOICE = {"audio_id": "587784541076604", "title": "Birthday Wish", "artist": "Shuba",
          "audio_volume": 80, "video_volume": 50}


# ── the stored choice ────────────────────────────────────────────────────────

def test_a_valid_choice_becomes_meta_audio_configuration():
    cfg = json.loads(IA.audio_configuration(CHOICE))
    assert cfg == {"audio_id": "587784541076604", "audio_volume": 80, "video_volume": 50}


@pytest.mark.parametrize("bad", [None, {}, "x", {"audio_id": ""}, {"audio_id": "12ab"}, {"audio_id": "1" * 41}])
def test_no_usable_choice_means_no_music(bad):
    assert IA.audio_configuration(bad) is None and IA.clean_choice(bad) is None


@pytest.mark.parametrize("given,expected", [(0, 1), (150, 100), (-5, 1), ("70", 70), ("loud", 100), (None, 100)])
def test_volumes_are_kept_within_metas_1_to_100(given, expected):
    assert IA.clean_choice({"audio_id": "1", "audio_volume": given})["audio_volume"] == expected


def test_volumes_default_to_full_like_metas_own_default():
    choice = IA.clean_choice({"audio_id": "1"})
    assert choice["audio_volume"] == 100 and choice["video_volume"] == 100


# ── search ───────────────────────────────────────────────────────────────────

SEARCH_ANSWER = {"audio": [
    {"audio_id": "1", "title": "Birthday Wish", "display_artist": "Shuba", "duration_in_ms": 153760,
     "cover_artwork_thumbnail_uri": "https://c/1.jpg", "download_url": "https://p/1.mp3"},
    {"audio_id": "2", "title": "No preview", "display_artist": "X", "duration_in_ms": 1000,
     "on_platform_audio_preview_link": "https://instagram/prev"},
    {"title": "no id, dropped"},
]}


def test_search_asks_for_music_for_this_account_and_normalises_the_tracks(http):
    http.answers = [(SEARCH_ANSWER,)]
    tracks = _run(IA.search_music("178414", "TOKEN", " birthday "))

    method, url, params = http.calls[0]
    assert url == f"{IA.AUDIO_BASE}/ig_audio"
    assert params == {"audio_type": "music", "user_id": "178414", "access_token": "TOKEN", "search_query": "birthday"}
    assert [t["audio_id"] for t in tracks] == ["1", "2"]
    assert tracks[0] == {"audio_id": "1", "title": "Birthday Wish", "artist": "Shuba", "duration_seconds": 154,
                         "cover_url": "https://c/1.jpg", "preview_url": "https://p/1.mp3"}
    assert tracks[1]["preview_url"] == "https://instagram/prev"


def test_search_with_no_query_asks_for_trending(http):
    http.answers = [({"audio": []},)]
    _run(IA.search_music("178414", "TOKEN", "  "))
    assert "search_query" not in http.calls[0][2]


def test_search_reports_instagrams_refusal_in_words(http):
    http.answers = [({"error": {"message": "(#10) Application does not have permission"}}, 400)]
    with pytest.raises(IA.AudioSearchError, match="Application does not have permission"):
        _run(IA.search_music("178414", "TOKEN"))


# ── scheduled / direct path ──────────────────────────────────────────────────

def _direct(audio):
    return _run(direct.InstagramDirectService.publish_reel(
        ig_user_id="178414", page_access_token="TOKEN", video_url="https://cdn/v.mp4", caption="hi", audio=audio))


def test_a_reel_with_a_track_carries_audio_configuration_on_the_audio_api_version(http):
    http.answers = [({"id": "C1"},), ({"id": "P1"},)]
    assert _direct(CHOICE)["success"] is True
    _, url, params = http.calls[0]
    assert url == f"{IA.AUDIO_BASE}/178414/media"
    assert json.loads(params["audio_configuration"])["audio_id"] == "587784541076604"
    assert params["media_type"] == "REELS" and params["video_url"] == "https://cdn/v.mp4"


def test_a_reel_without_a_track_is_exactly_as_before(http):
    http.answers = [({"id": "C1"},), ({"id": "P1"},)]
    assert _direct(None)["success"] is True
    _, url, params = http.calls[0]
    assert url == f"{direct.GRAPH_BASE}/178414/media" and "audio_configuration" not in params


def test_a_refused_track_fails_loudly_instead_of_posting_a_silent_reel(http):
    http.answers = [({"error": {"message": "Invalid audio_id"}},)]
    result = _direct(CHOICE)
    assert result["success"] is False
    assert "Invalid audio_id" in result["error"] and "music track" in result["error"]
    assert len(http.calls) == 1            # nothing was published


# ── immediate publish path (video drafts) ────────────────────────────────────

def _vps(audio):
    return _run(vps.VideoPublishService._publish_instagram_reel(
        "178414", "TOKEN", "https://cdn/v.mp4", "hi", audio))


def test_the_video_draft_path_attaches_the_track_too(http):
    http.answers = [({"id": "C1"},), ({"status_code": "FINISHED"},), ({"id": "M1"},)]
    media_id, _url = _vps(CHOICE)
    assert media_id == "M1"
    _, url, params = http.calls[0]
    assert url == f"{IA.AUDIO_BASE}/178414/media"
    assert json.loads(params["audio_configuration"]) == {"audio_id": "587784541076604", "audio_volume": 80,
                                                         "video_volume": 50}


def test_the_video_draft_path_without_a_track_is_unchanged(http):
    http.answers = [({"id": "C1"},), ({"status_code": "FINISHED"},), ({"id": "M1"},)]
    _vps(None)
    _, url, params = http.calls[0]
    assert url == f"{vps.GRAPH_BASE}/178414/media" and "audio_configuration" not in params


def test_the_video_draft_path_names_the_track_when_instagram_refuses_the_container(http):
    http.answers = [({"error": {"message": "Invalid audio_id"}},)]
    with pytest.raises(vps.MetaAPIError, match="music track was attached.*Invalid audio_id"):
        _vps(CHOICE)


# ── the API routes ───────────────────────────────────────────────────────────

class FakeCol:
    def __init__(self, docs):
        self.docs, self.updates = docs, []

    async def find_one(self, flt, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                return dict(d)
        return None

    async def update_one(self, flt, update):
        self.updates.append((flt, update))


class FakeDb:
    def __init__(self, **cols):
        self.cols = cols

    def __getitem__(self, name):
        return self.cols[name]


USER = {"claims": {"userId": "u1"}}


def _routes():
    from app.agents.social_media_manager.routers import complete_social_manager as R
    return R


def _set(db, audio, draft_id="d1", user=USER):
    R = _routes()
    return _run(R.set_instagram_audio(draft_id, R.InstagramAudioRequest(audio=audio), db=db, token=user))


def test_choosing_a_track_saves_it_on_the_reel_draft():
    drafts = FakeCol([{"id": "d1", "user_id": "u1", "video_url": "https://cdn/v.mp4"}])
    _set(FakeDb(content_drafts=drafts), CHOICE)
    flt, update = drafts.updates[0]
    assert flt == {"id": "d1", "user_id": "u1"}
    assert update["$set"]["instagram_audio"] == CHOICE


def test_choosing_none_removes_the_music():
    drafts = FakeCol([{"id": "d1", "user_id": "u1", "video_url": "https://cdn/v.mp4"}])
    _set(FakeDb(content_drafts=drafts), None)
    assert drafts.updates[0][1]["$set"]["instagram_audio"] is None


def test_music_cannot_be_added_to_an_image_post():
    from fastapi import HTTPException
    drafts = FakeCol([{"id": "d1", "user_id": "u1", "image_url": "https://cdn/i.jpg"}])
    with pytest.raises(HTTPException) as e:
        _set(FakeDb(content_drafts=drafts), CHOICE)
    assert e.value.status_code == 400 and "Reels" in e.value.detail and not drafts.updates


def test_someone_elses_draft_is_not_found():
    from fastapi import HTTPException
    drafts = FakeCol([{"id": "d1", "user_id": "someone-else", "video_url": "https://cdn/v.mp4"}])
    with pytest.raises(HTTPException) as e:
        _set(FakeDb(content_drafts=drafts), CHOICE)
    assert e.value.status_code == 404 and not drafts.updates


def test_an_invalid_track_id_is_rejected_before_it_is_stored():
    from pydantic import ValidationError
    R = _routes()
    with pytest.raises(ValidationError):
        R.InstagramAudioChoice(audio_id="not-a-number")
    with pytest.raises(ValidationError):
        R.InstagramAudioChoice(audio_id="1", audio_volume=0)


def _search(db, q=None):
    return _run(_routes().search_instagram_audio(q=q, db=db, token=USER))


def test_search_uses_the_users_instagram_connection(monkeypatch):
    seen = {}

    async def fake_search(ig_user_id, token, query):
        seen.update(ig=ig_user_id, token=token, query=query)
        return [{"audio_id": "1"}]

    monkeypatch.setattr(IA, "search_music", fake_search)
    conns = FakeCol([{"user_id": "u1", "platform": "instagram", "ig_user_id": "178414", "page_access_token": "TOK"}])
    result = _search(FakeDb(social_connections=conns), "birthday")
    assert seen == {"ig": "178414", "token": "TOK", "query": "birthday"}
    assert result["status"] is True and result["responseData"] == {"tracks": [{"audio_id": "1"}]}


def test_search_without_a_connected_instagram_says_to_connect_one():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _search(FakeDb(social_connections=FakeCol([])))
    assert e.value.status_code == 400 and "Connect" in e.value.detail


def test_search_passes_instagrams_refusal_on_as_a_readable_error(monkeypatch):
    from fastapi import HTTPException

    async def refuse(*a):
        raise IA.AudioSearchError("Instagram couldn't load music right now: no permission")

    monkeypatch.setattr(IA, "search_music", refuse)
    conns = FakeCol([{"user_id": "u1", "platform": "instagram", "ig_user_id": "1", "page_access_token": "T"}])
    with pytest.raises(HTTPException) as e:
        _search(FakeDb(social_connections=conns))
    assert e.value.status_code == 502 and "no permission" in e.value.detail
