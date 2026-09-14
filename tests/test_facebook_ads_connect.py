"""
Unit test for the Facebook Ads OAuth initiate redirect (complete_social_manager.py).

Live-diagnosed real bug: real completed connections showed Facebook silently
reusing whatever permission decision a user made on their FIRST-ever login,
even for later connects of entirely different Pages — 3 of 4 real brand
connections came back missing ads_management/pages_manage_ads even though the
scope was correctly requested every time. auth_type=rerequest is Facebook's own
documented fix: it forces every requested permission to be re-shown on every
call, regardless of past history.
"""
import asyncio
import urllib.parse
from unittest.mock import patch

from app.agents.social_media_manager.routers.complete_social_manager import facebook_ads_initiate


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _auth_url_params() -> dict:
    with patch.multiple(
        "app.agents.social_media_manager.routers.complete_social_manager.settings",
        META_APP_ID="app123",
        PUBLIC_API_URL="https://api-staging.urisocial.com",
        URI_GATEWAY_BASE_API_URL="",
        FACEBOOK_API_VERSION="v21.0",
    ):
        resp = _run(facebook_ads_initiate(source="settings"))
    location = resp.headers["location"]
    query = urllib.parse.urlparse(location).query
    return dict(urllib.parse.parse_qsl(query))


def test_initiate_requests_rerequest_so_facebook_always_reprompts():
    params = _auth_url_params()
    assert params.get("auth_type") == "rerequest"


def test_initiate_still_requests_the_full_ads_scope_set():
    params = _auth_url_params()
    scopes = set(params.get("scope", "").split(","))
    assert {"ads_management", "pages_manage_ads", "business_management"} <= scopes


def test_initiate_redirects_to_facebooks_real_oauth_dialog():
    params = _auth_url_params()
    with patch.multiple(
        "app.agents.social_media_manager.routers.complete_social_manager.settings",
        META_APP_ID="app123", PUBLIC_API_URL="https://api-staging.urisocial.com",
        URI_GATEWAY_BASE_API_URL="", FACEBOOK_API_VERSION="v21.0",
    ):
        resp = _run(facebook_ads_initiate(source="settings"))
    assert resp.headers["location"].startswith("https://www.facebook.com/v21.0/dialog/oauth?")
    assert params.get("client_id") == "app123"
