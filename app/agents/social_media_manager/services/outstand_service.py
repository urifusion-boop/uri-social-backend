import httpx
from typing import List, Optional, Dict, Any
from app.core.config import settings

OUTSTAND_BASE_URL = "https://api.outstand.so"


class OutstandPublishError(Exception):
    """
    Raised when Outstand rejects a publish request. Carries Outstand's own
    error message (e.g. "No social accounts found matching the provided
    account identifiers" — the signature of a connection that's been
    revoked/deleted on Outstand's side), not just the generic
    "400 Bad Request" httpx.raise_for_status() would otherwise produce.
    Callers need the real message to detect specific failure reasons and
    act on them (e.g. mark the connection disconnected).
    """

    def __init__(self, message: str, status_code: int):
        self.message = message
        self.status_code = status_code
        super().__init__(message)

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
        force_account_selection: bool = False,
    ) -> str:
        # force_account_selection maps to each network's own re-auth override
        # (disable_auto_auth for TikTok, auth_type=reauthenticate for Facebook,
        # force_reauth for Instagram) — without it, a browser with an existing
        # session on the network silently reuses whatever account is already
        # logged in instead of showing the picker/consent screen at all.
        body: Dict[str, Any] = {"tenant_id": tenant_id, "redirect_uri": redirect_uri}
        if force_account_selection:
            body["force_account_selection"] = True
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/social-networks/{network}/auth-url",
                headers=self.headers,
                json=body,
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

    async def get_upload_url(
        self,
        filename: str,
        content_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Request a presigned upload URL from Outstand's media API."""
        body: Dict[str, Any] = {"filename": filename}
        if content_type:
            body["content_type"] = content_type
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/media/upload",
                headers=self.headers,
                json=body,
            )
            resp.raise_for_status()
            return resp.json()["data"]

    async def confirm_upload(self, media_id: str, size: Optional[int] = None) -> Dict[str, Any]:
        """Mark a presigned upload as complete; returns the public media URL to attach to a post."""
        body: Dict[str, Any] = {}
        if size is not None:
            body["size"] = size
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/media/{media_id}/confirm",
                headers=self.headers,
                json=body,
            )
            resp.raise_for_status()
            return resp.json()["data"]

    async def upload_media_from_url(self, source_url: str) -> str:
        """
        Download media from an externally-hosted URL and re-upload it through
        Outstand's media API, returning an Outstand-hosted public URL.

        TikTok's pull_by_url media transfer requires the source domain to be
        verified in the TikTok developer portal — routing media through Outstand's
        own upload flow avoids that requirement since Outstand's media domain is
        already verified on their end.
        """
        filename = source_url.rsplit("/", 1)[-1].split("?")[0] or "media.mp4"

        async with httpx.AsyncClient(timeout=120.0) as client:
            source_resp = await client.get(source_url)
            source_resp.raise_for_status()
            file_bytes = source_resp.content

        upload_info = await self.get_upload_url(filename=filename)

        async with httpx.AsyncClient(timeout=120.0) as client:
            put_resp = await client.put(upload_info["upload_url"], content=file_bytes)
            put_resp.raise_for_status()

        confirmed = await self.confirm_upload(upload_info["id"], size=len(file_bytes))
        return confirmed["url"]

    async def get_post_analytics(self, post_id: str) -> Dict[str, Any]:
        """Fetch analytics for a published post from Outstand's GET /v1/posts/{id}/analytics."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/v1/posts/{post_id}/analytics",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_account_metrics(
        self,
        account_id: str,
        since: Optional[int] = None,
        until: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fetch account-level metrics from Outstand's GET /v1/social-accounts/{id}/metrics."""
        params: Dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/v1/social-accounts/{account_id}/metrics",
                headers=self.headers,
                params=params,
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
        platform_config: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        def _media_objects(urls: List[str]) -> List[Dict[str, str]]:
            result = []
            for u in urls:
                ext = u.rsplit(".", 1)[-1].split("?")[0].lower() if "." in u else "jpg"
                filename = u.rsplit("/", 1)[-1].split("?")[0] or f"image.{ext}"
                result.append({"url": u, "filename": filename})
            return result

        if tweets and len(tweets) > 1:
            containers = [{"content": t} for t in tweets]
            if media_urls:
                containers[0]["media"] = _media_objects(media_urls)
        else:
            container: Dict[str, Any] = {"content": content}
            if media_urls:
                container["media"] = _media_objects(media_urls)
            containers = [container]

        payload: Dict[str, Any] = {
            "accounts": outstand_account_ids,
            "containers": containers,
        }
        if scheduled_at:
            payload["scheduledAt"] = scheduled_at
        if platform_config:
            payload.update(platform_config)

        print(f"📡 Outstand POST /v1/posts/ payload keys={list(payload.keys())} containers={len(containers)} media={media_urls}")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/posts/",
                headers=self.headers,
                json=payload,
            )
            print(f"📡 Outstand response status: {resp.status_code} body: {resp.text[:2000]}")
            if resp.status_code >= 400:
                try:
                    error_body = resp.json()
                except Exception:
                    error_body = {}
                outstand_message = error_body.get("error") or resp.text[:500] or f"HTTP {resp.status_code}"
                raise OutstandPublishError(outstand_message, status_code=resp.status_code)
            return resp.json()
