"""
LinkedIn OAuth 2.0 direct posting service.

Flow:
  1. get_authorization_url(state, callback_url)  → redirect user to LinkedIn
  2. User authorises → LinkedIn redirects to callback_url?code=...&state=...
  3. exchange_code(code, callback_url)            → returns access token + profile info
  4. create_post(access_token, person_urn, text)  → publish to LinkedIn
"""

import urllib.parse
from typing import Any, Dict

import httpx

from app.core.config import settings

AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"

SCOPES = "openid profile email w_member_social"


class LinkedInDirectService:

    def __init__(self):
        self.client_id = settings.LINKEDIN_CLIENT_ID
        self.client_secret = settings.LINKEDIN_CLIENT_SECRET

    def get_authorization_url(self, state: str, callback_url: str) -> str:
        """Build the LinkedIn OAuth 2.0 authorization URL."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": callback_url,
            "state": state,
            "scope": SCOPES,
        }
        return f"{AUTHORIZATION_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, callback_url: str) -> Dict[str, Any]:
        """
        Exchange an authorization code for an access token.
        Returns the full token response (access_token, expires_in, ...).
        """
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            r.raise_for_status()
            return r.json()

    async def get_profile(self, access_token: str) -> Dict[str, Any]:
        """
        Fetch the authenticated member's profile via OpenID Connect userinfo.
        Returns {"sub", "name", "email", ...}.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            return r.json()

    async def create_post(
        self,
        access_token: str,
        person_urn: str,
        text: str,
    ) -> Dict[str, Any]:
        """
        Publish a text post to LinkedIn.
        person_urn: e.g. "urn:li:person:abc123"
        Returns {"post_id": ...}.
        """
        payload = {
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                UGC_POSTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            # LinkedIn returns 201 with the post URN in X-RestLi-Id header
            post_id = r.headers.get("x-restli-id") or r.headers.get("Location", "")
            return {"post_id": post_id}
