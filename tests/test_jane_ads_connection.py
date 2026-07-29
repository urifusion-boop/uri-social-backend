"""
Unit tests for the per-brand Meta ads connection state machine (ads_connection.py,
Per-Brand Page Connection plan). A fake db double stands in for Mongo — these prove
the six-state resolution logic and the pre-flight gate, not live Graph API behavior
(verify_token_live's own network call is mocked out per test).
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.ads_connection import (
    REQUIRED_ADS_SCOPES,
    AdsConnectionRequired,
    ConnectionState,
    resolve_ads_page_for_launch,
    resolve_connection_state,
    set_whatsapp_number,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _matches(self, doc, query):
        for key, val in query.items():
            if key == "$or":
                if not any(self._matches(doc, sub) for sub in val):
                    return False
            elif doc.get(key) != val:
                return False
        return True

    async def find_one(self, query, sort=None):
        matches = [d for d in self.docs if self._matches(d, query)]
        if not matches:
            return None
        return dict(matches[0])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return


class FakeDb:
    def __init__(self, docs=None):
        self._coll = FakeCollection(docs)

    def __getitem__(self, name):
        return self._coll


def _ads_doc(**kw):
    base = dict(
        id="fbads_pg123", platform="facebook_ads", connection_status="active",
        brand_id="brnd_1", page_id="pg123", page_access_token="tok",
        account_name="Brand Page", whatsapp_page_linked=False,
        whatsapp_number="",
    )
    base.update(kw)
    return base


def _content_doc(**kw):
    base = dict(id="fb_pg123", platform="facebook", brand_id="brnd_1")
    base.update(kw)
    return base


def test_none_when_no_connection_at_all():
    db = FakeDb([])
    state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.NONE
    assert ads is None


def test_content_only_when_posting_connected_but_no_ads():
    db = FakeDb([_content_doc()])
    state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.CONTENT_ONLY
    assert ads is None


def test_no_page_when_ads_connection_missing_page_id():
    db = FakeDb([_ads_doc(page_id="")])
    state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.NO_PAGE


def test_expired_when_live_check_fails():
    db = FakeDb([_ads_doc()])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(False, set()))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.EXPIRED


def test_expired_when_a_required_scope_is_missing():
    db = FakeDb([_ads_doc()])
    partial_scopes = REQUIRED_ADS_SCOPES - {"ads_management"}
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, partial_scopes))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.EXPIRED


def test_ads_no_whatsapp_when_number_not_linked():
    db = FakeDb([_ads_doc(whatsapp_page_linked=False)])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.ADS_NO_WHATSAPP


def test_ready_when_everything_is_in_place():
    db = FakeDb([_ads_doc(whatsapp_page_linked=True, whatsapp_number="2348031234567")])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.READY


def test_live_check_false_skips_the_network_call():
    db = FakeDb([_ads_doc(whatsapp_page_linked=True, whatsapp_number="2348031234567")])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(False, set()))) as mock_verify:
        state, ads = _run(resolve_connection_state(db, None, "brnd_1", live_check=False))
    assert state == ConnectionState.READY
    mock_verify.assert_not_called()


def test_resolve_ads_page_for_launch_raises_with_exact_state():
    db = FakeDb([_content_doc()])
    with pytest.raises(AdsConnectionRequired) as exc_info:
        _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert exc_info.value.state == ConnectionState.CONTENT_ONLY


def test_resolve_ads_page_for_launch_returns_page_and_whatsapp_when_ready():
    db = FakeDb([_ads_doc(whatsapp_page_linked=True, whatsapp_number="2348031234567")])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert result == {
        "page_id": "pg123",
        "whatsapp_number": "2348031234567",
        "page_name": "Brand Page",
    }


def test_set_whatsapp_number_normalizes_and_marks_linked():
    db = FakeDb([_ads_doc()])
    result = _run(set_whatsapp_number(db, None, "brnd_1", "0803 123 4567"))
    assert result == "2348031234567"
    stored = _run(db["social_connections"].find_one({"id": "fbads_pg123"}))
    assert stored["whatsapp_number"] == "2348031234567"
    assert stored["whatsapp_page_linked"] is True


def test_set_whatsapp_number_rejects_unparseable_input():
    db = FakeDb([_ads_doc()])
    with pytest.raises(ValueError):
        _run(set_whatsapp_number(db, None, "brnd_1", "not a number"))


def test_set_whatsapp_number_requires_an_existing_ads_connection():
    db = FakeDb([])
    with pytest.raises(AdsConnectionRequired) as exc_info:
        _run(set_whatsapp_number(db, None, "brnd_1", "0803 123 4567"))
    assert exc_info.value.state == ConnectionState.NONE
