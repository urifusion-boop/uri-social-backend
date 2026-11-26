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
    switch_account: bool = False,
) -> str:
    """The Facebook login/consent URL for connecting an account.

    A plain connect carries on as whichever Facebook account the browser is already signed into,
    with no password. Asking for the password every time (as this once did) was live-reported as
    a nuisance, so it is only asked for when the person chose "Use a different account":
    auth_type=reauthenticate makes Facebook ask for the password, and that screen is where they
    can switch ("Not you?" / log in as someone else).
    """
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": state,
        **({"auth_type": "reauthenticate"} if switch_account else {}),
        **(extra or {}),
    }
    return f"https://www.facebook.com/{api_version}/dialog/oauth?" + urllib.parse.urlencode(params)


async def facebook_person_name(client, graph_base: str, user_token: str) -> str:
    """The name of the Facebook person who signed in, shown on the connected card as
    "Connected by …" so the owner can tell which Facebook account a Page came through.
    Empty if Facebook doesn't say; connecting never fails over it."""
    try:
        r = await client.get(f"{graph_base}/me", params={"fields": "name", "access_token": user_token})
        return str((r.json() or {}).get("name") or "").strip()
    except Exception:
        return ""
