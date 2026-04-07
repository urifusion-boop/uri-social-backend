"""
X (Twitter) direct posting via OAuth 1.0a.
Works on the X free developer tier — no $100/month Basic plan needed.

Flow:
  1. get_request_token(callback_url)  → store temp tokens, redirect user to auth_url
  2. User authorises on X → X redirects to callback_url?oauth_token=...&oauth_verifier=...
  3. get_access_token(oauth_token, oauth_token_secret, oauth_verifier)
     → returns per-user access_token + access_token_secret (store in DB)
  4. post_tweet / post_thread using stored per-user tokens
"""

import asyncio
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests_oauthlib import OAuth1

from app.core.config import settings


class XDirectService:
    REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
    ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"
    AUTHORIZE_URL = "https://api.twitter.com/oauth/authorize"
    TWEETS_URL = "https://api.twitter.com/2/tweets"

    def __init__(self):
        self.api_key = settings.X_API_KEY
        self.api_secret = settings.X_API_SECRET

    def _auth(
        self,
        access_token: Optional[str] = None,
        access_token_secret: Optional[str] = None,
        **kwargs,
    ) -> OAuth1:
        return OAuth1(self.api_key, self.api_secret, access_token, access_token_secret, **kwargs)

    # ── Step 1: request token ────────────────────────────────────────────────

    async def get_request_token(self, callback_url: str) -> Tuple[str, str, str]:
        """
        Returns (oauth_token, oauth_token_secret, auth_url).
        Store oauth_token_secret temporarily (keyed by oauth_token) so you can
        complete the exchange in the callback.
        """
        def _call():
            auth = self._auth(callback_uri=callback_url)
            r = requests.post(self.REQUEST_TOKEN_URL, auth=auth, timeout=10)
            r.raise_for_status()
            return dict(urllib.parse.parse_qsl(r.text))

        loop = asyncio.get_running_loop()
        params = await loop.run_in_executor(None, _call)
        oauth_token = params["oauth_token"]
        oauth_token_secret = params["oauth_token_secret"]
        auth_url = f"{self.AUTHORIZE_URL}?oauth_token={oauth_token}"
        return oauth_token, oauth_token_secret, auth_url

    # ── Step 3: exchange for access token ────────────────────────────────────

    async def get_access_token(
        self,
        oauth_token: str,
        oauth_token_secret: str,
        oauth_verifier: str,
    ) -> Dict[str, str]:
        """
        Returns {"access_token", "access_token_secret", "user_id", "screen_name"}.
        Persist access_token + access_token_secret per user in social_connections.
        """
        def _call():
            auth = self._auth(oauth_token, oauth_token_secret, verifier=oauth_verifier)
            r = requests.post(self.ACCESS_TOKEN_URL, auth=auth, timeout=10)
            r.raise_for_status()
            return dict(urllib.parse.parse_qsl(r.text))

        loop = asyncio.get_running_loop()
        params = await loop.run_in_executor(None, _call)
        return {
            "access_token": params["oauth_token"],
            "access_token_secret": params["oauth_token_secret"],
            "user_id": params.get("user_id", ""),
            "screen_name": params.get("screen_name", ""),
        }

    # ── Step 4: publish ──────────────────────────────────────────────────────

    async def post_tweet(
        self,
        access_token: str,
        access_token_secret: str,
        text: str,
    ) -> Dict[str, Any]:
        """Post a single tweet. Returns the X API v2 response dict."""
        def _call():
            auth = self._auth(access_token, access_token_secret)
            r = requests.post(self.TWEETS_URL, auth=auth, json={"text": text}, timeout=15)
            r.raise_for_status()
            return r.json()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _call)

    async def post_thread(
        self,
        access_token: str,
        access_token_secret: str,
        tweets: List[str],
    ) -> List[Dict[str, Any]]:
        """
        Post a thread — each tweet replies to the previous one.
        Returns a list of X API v2 response dicts (one per tweet).
        """
        results: List[Dict[str, Any]] = []
        reply_to_id: Optional[str] = None

        for text in tweets:
            payload: Dict[str, Any] = {"text": text}
            if reply_to_id:
                payload["reply"] = {"in_reply_to_tweet_id": reply_to_id}

            captured_payload = payload  # capture for lambda closure

            def _call(p=captured_payload):
                auth = self._auth(access_token, access_token_secret)
                r = requests.post(self.TWEETS_URL, auth=auth, json=p, timeout=15)
                r.raise_for_status()
                return r.json()

            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _call)
            reply_to_id = (data.get("data") or {}).get("id")
            results.append(data)

        return results
