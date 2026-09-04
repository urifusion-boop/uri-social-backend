"""
Unit tests for audience_targeting.py — translating Jane's free-text audience call
(PlanVariant.audience_segment / the brand's target_audience) into Meta's actual
age_min/age_max/genders/flexible_spec targeting fields.

Live-reported bug this exists to fix: the real launched ad set always shipped
broad (all ages, all genders, no interests) regardless of Jane's audience
reasoning — that reasoning was only ever text, shown in the plan card, never
translated into what Meta's ad set accepts. openai and Meta's targeting-search
Graph endpoint are both mocked, mirroring test_jane_ads_plan_variants.py's pattern.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.jane_ads.audience_targeting import resolve_audience_targeting


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_openai_response(content: dict):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=json.dumps(content)))]
    return resp


def _mock_httpx_get(results_by_query: dict[str, dict]):
    """Meta's GET /search?type=adinterest — one canned response per `q` param,
    a fallback empty result for anything else."""
    client = AsyncMock()

    async def _get(url, params=None, **kw):
        q = (params or {}).get("q", "")
        r = MagicMock()
        r.json = lambda: results_by_query.get(q, {"data": []})
        return r

    client.get = AsyncMock(side_effect=_get)
    return client


def test_empty_text_returns_broad_no_api_calls():
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        assert _run(resolve_audience_targeting("   ", "tok")) == {}
    mock_client_cls.assert_not_called()


def test_no_openai_key_returns_broad():
    with patch("app.agents.jane_ads.audience_targeting.settings") as mock_settings:
        mock_settings.jane_ads_openai_key = ""
        assert _run(resolve_audience_targeting("young professionals", "tok")) == {}


def test_extracts_age_gender_and_resolves_interests():
    payload = {
        "age_min": 25, "age_max": 40, "gender": "female",
        "interest_keywords": ["Skincare", "Online shopping"],
    }
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    results = {
        "Skincare": {"data": [{"id": "6003139266461", "name": "Skincare"}]},
        "Online shopping": {"data": [{"id": "6003348604581", "name": "Online shopping"}]},
    }
    with patch("openai.AsyncOpenAI") as mock_client_cls, \
         patch("httpx.AsyncClient") as mock_http_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        mock_http = _mock_httpx_get(results)
        mock_http_cls.return_value.__aenter__.return_value = mock_http
        targeting = _run(resolve_audience_targeting("young women who shop online for skincare", "tok"))

    assert targeting["age_min"] == 25
    assert targeting["age_max"] == 40
    assert targeting["genders"] == [2]
    assert targeting["flexible_spec"] == [{"interests": [
        {"id": "6003139266461", "name": "Skincare"},
        {"id": "6003348604581", "name": "Online shopping"},
    ]}]


def test_no_age_or_gender_skew_omits_those_keys():
    # Most audience descriptions ("small businesses launching their first online
    # campaign") imply no demographic skew at all — must not invent one.
    payload = {"age_min": None, "age_max": None, "gender": "all", "interest_keywords": []}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        targeting = _run(resolve_audience_targeting(
            "small businesses launching their first online campaign", "tok"))
    assert targeting == {}


def test_out_of_range_age_is_ignored():
    # A model-hallucinated age outside Meta's sane ad-targeting bounds (or a
    # reversed range) must not reach the real ad set.
    payload = {"age_min": 10, "age_max": 90, "gender": "all", "interest_keywords": []}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        targeting = _run(resolve_audience_targeting("everyone, really", "tok"))
    assert "age_min" not in targeting and "age_max" not in targeting


def test_unresolvable_interest_is_dropped_not_invented():
    payload = {"age_min": None, "age_max": None, "gender": "all",
               "interest_keywords": ["Not a real interest at all"]}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    with patch("openai.AsyncOpenAI") as mock_client_cls, \
         patch("httpx.AsyncClient") as mock_http_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        mock_http = _mock_httpx_get({})  # no query matches → empty results
        mock_http_cls.return_value.__aenter__.return_value = mock_http
        targeting = _run(resolve_audience_targeting("a very made-up niche", "tok"))
    assert "flexible_spec" not in targeting


def test_one_bad_interest_lookup_does_not_block_the_others():
    payload = {"age_min": None, "age_max": None, "gender": "all",
               "interest_keywords": ["Broken keyword", "Small business"]}
    mock_create = AsyncMock(return_value=_mock_openai_response(payload))
    client = AsyncMock()

    async def _get(url, params=None, **kw):
        if params.get("q") == "Broken keyword":
            raise RuntimeError("Graph API down for this one")
        r = MagicMock()
        r.json = lambda: {"data": [{"id": "6003107902433", "name": "Small business"}]}
        return r

    client.get = AsyncMock(side_effect=_get)
    with patch("openai.AsyncOpenAI") as mock_client_cls, \
         patch("httpx.AsyncClient") as mock_http_cls:
        mock_client_cls.return_value.chat.completions.create = mock_create
        mock_http_cls.return_value.__aenter__.return_value = client
        targeting = _run(resolve_audience_targeting("small business owners", "tok"))
    assert targeting["flexible_spec"] == [{"interests": [{"id": "6003107902433", "name": "Small business"}]}]


def test_openai_outage_returns_broad_never_raises():
    with patch("openai.AsyncOpenAI", side_effect=Exception("down")):
        targeting = _run(resolve_audience_targeting("young professionals", "tok"))
    assert targeting == {}
