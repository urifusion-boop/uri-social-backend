"""
The URL a user is sent to when connecting a TikTok account directly.

TikTok silently approves whichever account the browser is already logged into unless the request
carries disable_auto_auth=1, so a person could never connect a different account. These tests pin
that parameter and the rest of what TikTok's authorization request needs.
"""
import urllib.parse

from app.agents.social_media_manager.services.tiktok_direct_service import build_authorize_url

REDIRECT = "https://api.example.com/social-media/connect/tiktok-direct/callback"


def _query(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_it_always_shows_tiktoks_authorization_page_so_the_account_can_be_changed():
    assert _query(build_authorize_url("ck", REDIRECT, "settings"))["disable_auto_auth"] == "1"


def test_it_carries_everything_tiktok_requires():
    url = build_authorize_url("ck", REDIRECT, "settings")
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    q = _query(url)
    assert q["client_key"] == "ck"
    assert q["redirect_uri"] == REDIRECT
    assert q["response_type"] == "code"
    assert q["scope"] == "user.info.basic,video.publish"
    assert q["state"] == "settings"
