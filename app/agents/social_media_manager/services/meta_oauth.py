"""
The Facebook Login URL a user is sent to when connecting a Facebook Page or an Instagram account.

Both connections run through Facebook's own login dialog, so they share this builder.
"""
from __future__ import annotations

import urllib.parse
from typing import Dict, List, Optional


def build_authorize_url(
    app_id: str,
    redirect_uri: str,
    scopes: List[str],
    state: str,
    api_version: str,
    extra: Optional[Dict[str, str]] = None,
) -> str:
    """The Facebook login/consent URL for connecting an account.

    auth_type=reauthenticate makes Facebook ask for the password again instead of silently continuing
    as whichever account the browser is already logged into, which is where someone who wants to
    connect a different account gets to switch ("Not you?" / log in as someone else). Without it a
    browser that is already signed in and has already approved the app goes straight through, so the
    person never gets the chance.
    """
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": state,
        "auth_type": "reauthenticate",
        **(extra or {}),
    }
    return f"https://www.facebook.com/{api_version}/dialog/oauth?" + urllib.parse.urlencode(params)
