"""
The URL a user is sent to when connecting a Facebook Page or an Instagram account.

Facebook silently continues as whichever account the browser is already logged into, and skips its
dialog entirely for an app already approved, unless the request carries auth_type=reauthenticate, so a
person could never connect a different account. These tests pin that parameter and the rest of what
the dialog needs.
"""
import urllib.parse

from app.agents.social_media_manager.services.meta_oauth import build_authorize_url

REDIRECT = "https://api.example.com/social-media/connect/facebook-direct/callback"


def _query(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_it_asks_for_the_password_again_so_the_account_can_be_changed():
    url = build_authorize_url("app", REDIRECT, ["pages_show_list"], "settings", "v21.0")
    assert _query(url)["auth_type"] == "reauthenticate"


def test_it_carries_everything_facebook_requires():
    url = build_authorize_url("app", REDIRECT, ["pages_show_list", "pages_manage_posts"], "inbox", "v21.0")
    assert url.startswith("https://www.facebook.com/v21.0/dialog/oauth?")
    q = _query(url)
    assert q["client_id"] == "app"
    assert q["redirect_uri"] == REDIRECT
    assert q["response_type"] == "code"
    assert q["scope"] == "pages_show_list,pages_manage_posts"
    assert q["state"] == "inbox"


def test_extra_parameters_are_added():
    url = build_authorize_url("app", REDIRECT, ["x"], "settings", "v20.0", {"display": "page"})
    q = _query(url)
    assert q["display"] == "page"
    assert q["auth_type"] == "reauthenticate"
