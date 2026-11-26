"""
The URL a user is sent to when connecting a Facebook Page or an Instagram account.

A plain Connect carries on as the Facebook account the browser is signed into, with no password
(asking every time was live-reported as a nuisance). "Use a different account" sends
auth_type=reauthenticate, so Facebook asks for the password and offers to switch accounts. These
tests pin both, the rest of what the dialog needs, and the "Connected by" name.
"""
import urllib.parse

from app.agents.social_media_manager.services.meta_oauth import build_authorize_url

REDIRECT = "https://api.example.com/social-media/connect/facebook-direct/callback"


def _query(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_a_plain_connect_does_not_ask_for_the_password():
    url = build_authorize_url("app", REDIRECT, ["pages_show_list"], "settings", "v21.0")
    assert "auth_type" not in _query(url)


def test_using_a_different_account_asks_for_the_password_so_it_can_be_changed():
    url = build_authorize_url("app", REDIRECT, ["pages_show_list"], "settings", "v21.0", switch_account=True)
    assert _query(url)["auth_type"] == "reauthenticate"


def test_both_connect_buttons_pass_the_switch_through(monkeypatch):
    import asyncio
    from app.agents.social_media_manager.routers import complete_social_manager as csm
    monkeypatch.setattr(csm.settings, "META_APP_ID", "app", raising=False)
    run = asyncio.get_event_loop().run_until_complete

    fb = lambda **kw: csm.facebook_direct_initiate(source="settings", rerequest=0, **kw)
    ig = lambda **kw: csm.instagram_direct_initiate(source="settings", **kw)
    for initiate in (fb, ig):
        plain = _query(run(initiate(switch=0)).headers["location"])
        switched = _query(run(initiate(switch=1)).headers["location"])
        assert "auth_type" not in plain
        assert switched["auth_type"] == "reauthenticate" and switched["state"] == "settings"


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
    assert "auth_type" not in q


def test_facebook_reconnect_re_asks_for_a_switched_off_permission(monkeypatch):
    import asyncio
    from app.agents.social_media_manager.routers import complete_social_manager as csm
    monkeypatch.setattr(csm.settings, "META_APP_ID", "app", raising=False)

    resp = asyncio.get_event_loop().run_until_complete(
        csm.facebook_direct_initiate(source="inbox", rerequest=1, switch=1))   # rerequest wins
    q = _query(resp.headers["location"])
    assert q["auth_type"] == "rerequest"
    assert q["state"] == "inbox|rerequested"
    assert "pages_manage_metadata" in q["scope"]


def test_declined_scopes_reads_only_switched_off_permissions():
    import asyncio
    from types import SimpleNamespace
    from app.agents.social_media_manager.routers.complete_social_manager import declined_scopes

    class _Client:
        async def get(self, url, params=None):
            return SimpleNamespace(json=lambda: {"data": [
                {"permission": "pages_messaging", "status": "granted"},
                {"permission": "pages_manage_metadata", "status": "declined"}]})

    out = asyncio.get_event_loop().run_until_complete(declined_scopes(_Client(), "g", "t"))
    assert out == ["pages_manage_metadata"]


def test_the_person_who_signed_in_is_named_and_a_failure_is_just_blank():
    import asyncio
    from types import SimpleNamespace
    from app.agents.social_media_manager.services.meta_oauth import facebook_person_name
    run = asyncio.get_event_loop().run_until_complete

    class _Client:
        def __init__(self, body=None, boom=False):
            self.body, self.boom, self.calls = body, boom, []

        async def get(self, url, params=None):
            self.calls.append((url, params))
            if self.boom:
                raise RuntimeError("network")
            return SimpleNamespace(json=lambda: self.body)

    ok = _Client({"id": "1", "name": " Uche Theo "})
    assert run(facebook_person_name(ok, "https://graph.facebook.com/v21.0", "tok")) == "Uche Theo"
    assert ok.calls[0][0].endswith("/me") and ok.calls[0][1]["fields"] == "name"
    assert run(facebook_person_name(_Client({"error": {}}), "g", "t")) == ""
    assert run(facebook_person_name(_Client(boom=True), "g", "t")) == ""
