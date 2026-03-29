import httpx
from typing import List, Optional, Dict, Any
from app.core.config import settings

OUTSTAND_BASE_URL = "https://api.outstand.so"

# Maps our internal platform names → Outstand network identifiers
PLATFORM_TO_NETWORK: Dict[str, str] = {
    "facebook":       "facebook",
    "instagram":      "instagram",
    "linkedin":       "linkedin",
    "twitter":        "x",
    "x":              "x",
    "tiktok":         "tiktok",
    "youtube":        "youtube",
    "pinterest":      "pinterest",
    "threads":        "threads",
    "bluesky":        "bluesky",
    "google_business": "google_business",
}

SUPPORTED_PLATFORMS = set(PLATFORM_TO_NETWORK.keys())


class OutstandService:
    """Thin async wrapper around the Outstand REST API."""

    def __init__(self):
        self.api_key = settings.OUTSTAND_API_KEY
        self.base_url = OUTSTAND_BASE_URL
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self.timeout = 30.0

    async def configure_network(
        self,
        network: str,
        client_key: str,
        client_secret: str,
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/social-networks",
                headers=self.headers,
                json={"network": network, "client_key": client_key, "client_secret": client_secret},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_auth_url(
        self,
        network: str,
        tenant_id: str,
        redirect_uri: str,
    ) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/social-networks/{network}/auth-url",
                headers=self.headers,
                json={"tenant_id": tenant_id, "redirect_uri": redirect_uri},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"]["auth_url"]

    async def get_pending_connection(self, session_token: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/v1/social-accounts/pending/{session_token}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def finalize_connection(
        self,
        session_token: str,
        selected_page_ids: List[str],
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/social-accounts/pending/{session_token}/finalize",
                headers=self.headers,
                json={"selectedPageIds": selected_page_ids},
            )
            resp.raise_for_status()
            return resp.json()

    async def list_accounts(
        self,
        tenant_id: str,
        network: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"tenantId": tenant_id, "limit": limit}
        if network:
            params["network"] = network

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/v1/social-accounts",
                headers=self.headers,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_account(self, outstand_account_id: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.delete(
                f"{self.base_url}/v1/social-accounts/{outstand_account_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_post_analytics(self, post_id: str) -> Dict[str, Any]:
        """Fetch analytics for a published post from Outstand's GET /v1/posts/{id}/analytics."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/v1/posts/{post_id}/analytics",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_post(self, post_id: str) -> Dict[str, Any]:
        """Fetch the current status/details of a post from Outstand's GET /v1/posts/{id}."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/v1/posts/{post_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def publish_post(
        self,
        outstand_account_ids: List[str],
        content: str,
        scheduled_at: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        tweets: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if tweets and len(tweets) > 1:
            containers = [{"content": t} for t in tweets]
            if media_urls:
                containers[0]["media"] = [{"url": u} for u in media_urls]
        else:
            container: Dict[str, Any] = {"content": content}
            if media_urls:
                container["media"] = [{"url": u} for u in media_urls]
            containers = [container]

        payload: Dict[str, Any] = {
            "accounts": outstand_account_ids,
            "containers": containers,
        }
        if scheduled_at:
            payload["scheduledAt"] = scheduled_at

        print(f"📡 Outstand POST /v1/posts/ payload keys={list(payload.keys())} containers={len(containers)} media={media_urls}")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/posts/",
                headers=self.headers,
                json=payload,
            )
            print(f"📡 Outstand response status: {resp.status_code} body: {resp.text[:500]}")
            resp.raise_for_status()
            return resp.json()
