"""
LinkedIn OAuth 2.0 direct posting service.

Flow:
  1. get_authorization_url(state, callback_url)  → redirect user to LinkedIn
  2. User authorises → LinkedIn redirects to callback_url?code=...&state=...
  3. exchange_code(code, callback_url)            → returns access token + profile info
  4. get_admin_pages(access_token)               → returns company pages user admins
  5. create_post(access_token, author_urn, text)  → publish to LinkedIn (person or org)
"""

import urllib.parse
from typing import Any, Dict

import httpx

from app.core.config import settings

AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"
ORG_ACLS_URL = "https://api.linkedin.com/v2/organizationAcls"

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

    async def get_admin_pages(self, access_token: str) -> list:
        """
        Fetch all LinkedIn company pages where the authenticated user is an ADMINISTRATOR.
        Returns a list of {"id": str, "name": str, "urn": str}.
        """
        params = {
            "q": "roleAssignee",
            "role": "ADMINISTRATOR",
            "projection": "(elements*(organization~(id,localizedName)))",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                ORG_ACLS_URL,
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                },
            )
            if r.status_code != 200:
                return []
            data = r.json()

        pages = []
        for element in data.get("elements", []):
            org = element.get("organization~", {})
            org_id = str(org.get("id", ""))
            name = org.get("localizedName", "")
            if org_id:
                pages.append({
                    "id": org_id,
                    "name": name,
                    "urn": f"urn:li:organization:{org_id}",
                })
        return pages

    async def _register_image(self, access_token: str, author_urn: str) -> Dict[str, Any]:
        """Register an image upload with LinkedIn. Returns upload URL and asset URN."""
        payload = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": author_urn,
                "serviceRelationships": [
                    {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                ],
            }
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.linkedin.com/v2/assets?action=registerUpload",
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
            upload_url = data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
            asset = data["value"]["asset"]
            return {"upload_url": upload_url, "asset": asset}

    async def create_post(
        self,
        access_token: str,
        person_urn: str,
        text: str,
        image_url: str = None,
    ) -> Dict[str, Any]:
        """
        Publish a text (or image+text) post to LinkedIn.
        person_urn: e.g. "urn:li:person:abc123"
        image_url: optional public URL of image to attach
        Returns {"post_id": ...}.
        """
        media_category = "NONE"
        media = []

        if image_url:
            try:
                # 1. Register upload
                reg = await self._register_image(access_token, person_urn)
                upload_url = reg["upload_url"]
                asset = reg["asset"]

                # 2. Download image and upload to LinkedIn
                async with httpx.AsyncClient(timeout=60) as client:
                    img_resp = await client.get(image_url)
                    img_resp.raise_for_status()
                    await client.put(
                        upload_url,
                        content=img_resp.content,
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": img_resp.headers.get("content-type", "image/jpeg"),
                        },
                    )

                media_category = "IMAGE"
                media = [{
                    "status": "READY",
                    "description": {"text": ""},
                    "media": asset,
                    "title": {"text": ""},
                }]
            except Exception as e:
                print(f"[LinkedIn] image upload failed, posting text only: {e}")
                media_category = "NONE"
                media = []

        share_content: Dict[str, Any] = {
            "shareCommentary": {"text": text},
            "shareMediaCategory": media_category,
        }
        if media:
            share_content["media"] = media

        payload = {
            "author": person_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
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
            post_id = r.headers.get("x-restli-id") or r.headers.get("Location", "")
            return {"post_id": post_id}
