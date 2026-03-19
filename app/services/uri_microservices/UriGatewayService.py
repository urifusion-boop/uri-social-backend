from datetime import timedelta
from typing import Optional
import httpx
from app.core.config import settings
from app.database import get_db
from app.domain.enums.endpoints_enum import UriBackendEndpointsEnum
from app.domain.enums.microservicestype_enum import MicroServiceTypeEnum
from app.domain.enums.subscription_enum import SubscriptionStatusEnum
from app.domain.enums.urigatewayendpoints_enum import UriGatewayEndpointsEnum
from app.repository.CacheRepository import CacheRepository


class UriGatewayService:
    base_url = settings.URI_GATEWAY_BASE_API_URL

    @staticmethod
    def _construct_url(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{UriGatewayService.base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _get_auth_headers(auth_token: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        return headers

    @staticmethod
    async def _get_uri_oauth_token() -> str:
        cached_token = await CacheRepository.get_cache(get_db(), "authToken")
        if cached_token:
            print("Auth token found in cache.")
            return cached_token
        token_request = {
            "clientId": settings.URI_CLIENT_ID,
            "clientSecret": settings.URI_CLIENT_SECRET,
            "grantType": "client_credentials",
            "clientStatus": SubscriptionStatusEnum.ACTIVE.value,
            "serviceType": MicroServiceTypeEnum.URI_INSIGHTS.value,
        }

        url = UriGatewayService._construct_url(
            UriGatewayEndpointsEnum.URI_BACKEND.value
            + UriBackendEndpointsEnum.URI_BACKEND_OAUTH_TOKEN.value
        )

        headers = {"Content-Type": "application/json"}

        async with httpx.AsyncClient() as client:
            response = await client.post(url=url, json=token_request, headers=headers)
            response.raise_for_status()
            data = (response.json()).get("responseData", {})
            auth_token = data.get("access_token", "")
            ttl = data.get("expires_in", 3600)

            await CacheRepository.set_cache(
                get_db(),
                "authToken",
                auth_token,
                ttl=timedelta(seconds=int(ttl) if isinstance(ttl, str) else ttl),
            )
            return auth_token

    @staticmethod
    async def _request(
        method: str,
        url: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
        max_retries: int = 3,
    ):
        auth_token = await UriGatewayService._get_uri_oauth_token()
        headers = UriGatewayService._get_auth_headers(auth_token)
        constructed_url = UriGatewayService._construct_url(url)
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method=method.upper(),
                        url=constructed_url,
                        json=data,
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()
                    return response.json()

            except httpx.HTTPStatusError as e:
                print(
                    f"[Attempt {attempt + 1}/{max_retries}] HTTP error {e.response.status_code}: ",
                    e.response.text,
                )
                if attempt == max_retries - 1:
                    return None

            except httpx.RequestError as e:
                print(
                    f"[Attempt {attempt + 1}/{max_retries}] Connection error: ",
                    e,
                )
                if attempt == max_retries - 1:
                    return None

    @staticmethod
    async def post(url: str, data: Optional[dict] = None, params: Optional[dict] = None, max_retries: int = 3):
        return await UriGatewayService._request("POST", url=url, data=data, params=params, max_retries=max_retries)

    @staticmethod
    async def get(url: str, params: dict = {}, max_retries: int = 3):
        return await UriGatewayService._request("GET", url=url, params=params, max_retries=max_retries)

    @staticmethod
    async def put(url: str, data: Optional[dict] = None, params: Optional[dict] = None, max_retries: int = 3):
        return await UriGatewayService._request("PUT", url=url, data=data, params=params, max_retries=max_retries)

    @staticmethod
    async def delete(url: str, params: Optional[dict] = None, max_retries: int = 3):
        return await UriGatewayService._request("DELETE", url=url, params=params, max_retries=max_retries)
