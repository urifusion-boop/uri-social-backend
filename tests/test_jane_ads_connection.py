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
    verify_token_live,
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


def test_expired_when_business_manager_share_genuinely_failed():
    # A real share failure still blocks: the shared ad account cannot advertise for a
    # Page it was never granted, so letting this reach READY would fail at ad time.
    db = FakeDb([_ads_doc(
        business_manager_shared=False,
        business_manager_error="Business Manager page-share failed: insufficient permissions.",
    )])
    state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.EXPIRED
    assert "insufficient permissions" in ads["_business_manager_error"]


def test_duplicate_asset_share_error_is_treated_as_already_shared():
    # "You are trying to assign a duplicated asset" is Meta refusing to re-share a Page
    # the Business Manager ALREADY has — it reads like a failure and is stored as
    # business_manager_shared=False, but the Page is in fact assigned. Live-confirmed:
    # a Page with this exact error advertised fine, while a different Page with
    # business_manager_shared=True was rejected at ad-creative time. Blocking on it
    # would push a working brand Page back onto the shared URI Page.
    db = FakeDb([_ads_doc(
        business_manager_shared=False,
        business_manager_error="Business Manager page-share failed: You are trying to assign a duplicated asset to this agency.",
        whatsapp_page_linked=True, whatsapp_number="2348031234567",
    )])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, _ = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.READY


def test_ready_when_business_manager_shared_is_true():
    db = FakeDb([_ads_doc(
        business_manager_shared=True, whatsapp_page_linked=True, whatsapp_number="2348031234567",
    )])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.READY


def test_ready_when_business_manager_shared_field_is_missing_entirely():
    # Older connections predate this field being tracked at all — absence isn't
    # itself a red flag, only an explicit False is.
    db = FakeDb([_ads_doc(whatsapp_page_linked=True, whatsapp_number="2348031234567")])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.READY


def test_business_manager_share_failure_is_checked_before_the_live_network_call():
    db = FakeDb([_ads_doc(business_manager_shared=False, business_manager_error="dup asset")])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))) as mock_verify:
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.EXPIRED
    mock_verify.assert_not_called()


def test_verify_token_live_calls_permissions_with_the_user_token_not_the_page_token():
    # Live-confirmed: GET /me/permissions returns an empty list for a Page access token
    # every time, regardless of what was actually granted — it only returns real scope
    # data for a USER access token. Passing the page token there silently broke every
    # connection's health check (always EXPIRED). This locks in the fix: the /me/permissions
    # call must use user_access_token when one is on file, not page_access_token.
    calls = []

    class FakeResp:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data

    async def fake_get(url, params=None):
        calls.append((url, params))
        if url.endswith("/pg123"):
            return FakeResp({"id": "pg123"})
        return FakeResp({"data": [{"permission": "ads_management", "status": "granted"}]})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=fake_get)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        valid, granted = _run(verify_token_live("pg123", "page-tok", "user-tok"))

    assert valid is True
    assert granted == {"ads_management"}
    perms_call = next(c for c in calls if "permissions" in c[0])
    assert perms_call[1]["access_token"] == "user-tok"


def test_verify_token_live_falls_back_to_page_token_when_no_user_token_on_file():
    calls = []

    class FakeResp:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data

    async def fake_get(url, params=None):
        calls.append((url, params))
        if url.endswith("/pg123"):
            return FakeResp({"id": "pg123"})
        return FakeResp({"data": []})

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=fake_get)
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(verify_token_live("pg123", "page-tok", ""))

    perms_call = next(c for c in calls if "permissions" in c[0])
    assert perms_call[1]["access_token"] == "page-tok"


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


def test_expired_carries_which_scopes_are_missing():
    # Live-reported: a token can be valid but Facebook still doesn't grant every
    # requested permission — surface exactly which ones, not just "expired".
    db = FakeDb([_ads_doc()])
    partial_scopes = REQUIRED_ADS_SCOPES - {"ads_management", "pages_manage_ads"}
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, partial_scopes))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.EXPIRED
    assert ads["_missing_scopes"] == ["ads_management", "pages_manage_ads"]


def test_expired_from_invalid_token_reports_no_specific_missing_scopes():
    # An invalid/dead token isn't a "missing scope" situation — nothing was read at
    # all, so there's nothing specific to report.
    db = FakeDb([_ads_doc()])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(False, set()))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.EXPIRED
    assert ads["_missing_scopes"] == []


def test_ads_no_whatsapp_when_number_not_linked():
    db = FakeDb([_ads_doc(whatsapp_page_linked=False)])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1"))
    assert state == ConnectionState.ADS_NO_WHATSAPP


def test_ready_when_whatsapp_not_linked_but_not_required():
    # A followers/engagement campaign never routes through WhatsApp — the Page
    # connection alone is READY for it, whatsapp_page_linked or not.
    db = FakeDb([_ads_doc(whatsapp_page_linked=False)])
    with patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        state, ads = _run(resolve_connection_state(db, None, "brnd_1", require_whatsapp=False))
    assert state == ConnectionState.READY


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


def test_resolve_ads_page_for_launch_uses_the_shared_page_regardless_of_any_connection():
    # Every brand launches from URI's own shared Page — a brand with NO Facebook
    # connection of any kind (not even content) still launches fine, as long as
    # they've saved their own WhatsApp number.
    db = FakeDb([{"brand_id": "brnd_1", "whatsapp_number": "2348031234567"}])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert result == {"page_id": "pg_shared", "whatsapp_number": "2348031234567", "page_name": "URI Social"}


def test_resolve_ads_page_for_launch_raises_ads_no_whatsapp_when_brand_has_no_number_saved():
    db = FakeDb([])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"):
        with pytest.raises(AdsConnectionRequired) as exc_info:
            _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert exc_info.value.state == ConnectionState.ADS_NO_WHATSAPP


def test_resolve_ads_page_for_launch_skips_whatsapp_when_not_required():
    db = FakeDb([])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1", require_whatsapp=False))
    assert result == {"page_id": "pg_shared", "whatsapp_number": "", "page_name": "URI Social"}


def test_resolve_ads_page_for_launch_surfaces_the_real_state_with_no_shared_page():
    # Nothing to fall back to, so the brand's own connection state is what the caller
    # needs in order to prompt precisely (connect vs reconnect vs no Page).
    db = FakeDb([{"brand_id": "brnd_1", "whatsapp_number": "2348031234567"}])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", ""):
        with pytest.raises(AdsConnectionRequired) as exc_info:
            _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert exc_info.value.state == ConnectionState.NONE


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


# ── Per-brand Page preference ─────────────────────────────────────────────────
# Ads should publish under the CLIENT's own Page when they've connected one, so every
# client's ad doesn't read "URI". Brands without a connection still fall back to the
# shared Page so nobody is blocked from launching.

def test_launch_prefers_the_brands_own_connected_page_over_the_shared_one():
    db = FakeDb([
        {"brand_id": "brnd_1", "whatsapp_number": "2348031234567"},
        _ads_doc(page_id="pg_brand", account_name="Precious Cakes",
                 business_manager_shared=True),
    ])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"), \
         patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert result["page_id"] == "pg_brand"
    assert result["page_name"] == "Precious Cakes"
    # The wa.me destination still comes from the brand's own saved number.
    assert result["whatsapp_number"] == "2348031234567"


def test_launch_falls_back_to_the_shared_page_when_the_brand_has_not_connected():
    db = FakeDb([{"brand_id": "brnd_1", "whatsapp_number": "2348031234567"}])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert result["page_id"] == "pg_shared"
    assert result["page_name"] == "URI Social"


def test_an_unusable_brand_connection_falls_back_instead_of_blocking_the_launch():
    # A dead/expired token on the brand's Page must not stop them advertising — the
    # shared Page still works, and blocking would be worse than a generic identity.
    db = FakeDb([
        {"brand_id": "brnd_1", "whatsapp_number": "2348031234567"},
        _ads_doc(page_id="pg_brand"),
    ])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"), \
         patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(False, set()))):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert result["page_id"] == "pg_shared"


def test_brand_page_is_used_even_when_its_whatsapp_was_never_linked_in_meta():
    # wa.me routing needs no Meta-side number linking, so whatsapp_page_linked must not
    # gate the brand's own Page any more (it used to, via require_whatsapp).
    db = FakeDb([
        {"brand_id": "brnd_1", "whatsapp_number": "2348031234567"},
        _ads_doc(page_id="pg_brand", account_name="Brand Page",
                 whatsapp_page_linked=False, business_manager_shared=True),
    ])
    with patch("app.agents.jane_ads.ads_connection.settings.META_ADS_PAGE_ID", "pg_shared"), \
         patch("app.agents.jane_ads.ads_connection.verify_token_live",
               new=AsyncMock(return_value=(True, REQUIRED_ADS_SCOPES))):
        result = _run(resolve_ads_page_for_launch(db, None, "brnd_1"))
    assert result["page_id"] == "pg_brand"
