"""
Unit test for the Instagram-direct OAuth initiate redirect (complete_social_manager.py).

Live-reported bug (2026-09-21): some users on mobile Chrome (not an embedded/
in-app browser) never reach our redirect_uri at all when connecting Instagram —
Facebook's dialog/oauth (Instagram uses Facebook Login under the hood) appears to
hand off to the native Facebook app instead of rendering the web dialog on some
devices, when no `display` mode is explicitly requested. `display=page` asks for
the full-page web dialog explicitly, the correct mode for this server-redirect
flow, instead of leaving Facebook/the OS to auto-detect.
"""
import asyncio
import urllib.parse
from unittest.mock import patch

from app.agents.social_media_manager.routers.complete_social_manager import instagram_direct_initiate


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _auth_url_params() -> dict:
    with patch.multiple(
        "app.agents.social_media_manager.routers.complete_social_manager.settings",
        META_APP_ID="app123",
        PUBLIC_API_URL="https://api-staging.urisocial.com",
        URI_GATEWAY_BASE_API_URL="",
    ):
        resp = _run(instagram_direct_initiate(source="settings"))
    location = resp.headers["location"]
    query = urllib.parse.urlparse(location).query
    return dict(urllib.parse.parse_qsl(query))


def test_initiate_requests_full_page_display_not_auto_detected():
    params = _auth_url_params()
    assert params.get("display") == "page"


def test_initiate_still_requests_instagram_scopes():
    params = _auth_url_params()
    scopes = set(params.get("scope", "").split(","))
    assert {"instagram_basic", "instagram_content_publish"} <= scopes


def test_initiate_redirects_to_facebooks_real_oauth_dialog():
    with patch.multiple(
        "app.agents.social_media_manager.routers.complete_social_manager.settings",
        META_APP_ID="app123", PUBLIC_API_URL="https://api-staging.urisocial.com",
        URI_GATEWAY_BASE_API_URL="",
    ):
        resp = _run(instagram_direct_initiate(source="settings"))
    assert resp.headers["location"].startswith("https://www.facebook.com/v20.0/dialog/oauth?")
