"""
Minimal FacebookService stub for the uri-agent.
Only implements the two methods used by ApprovalWorkflowService.
"""
import json
import httpx
from http import HTTPStatus
from typing import Any, Dict

from app.core.config import settings
from app.domain.responses.uri_response import UriResponse


class FacebookService:
    @staticmethod
    async def post_on_facebook(page_id: str, post_data: dict, access_token: str):
        """
        Posts content on Facebook using the Graph API.
        Sends access_token as query param + form-encoded body (not Bearer + JSON)
        to avoid Facebook error code 1 on certain token types.
        """
        url = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/feed"
        params = {"access_token": access_token}

        form: Dict[str, Any] = {
            "message": post_data.get("message", ""),
            "published": str(post_data.get("published", True)).lower(),
        }
        if post_data.get("attached_media"):
            form["attached_media"] = json.dumps(post_data["attached_media"])
        if post_data.get("scheduled_publish_time"):
            form["scheduled_publish_time"] = str(post_data["scheduled_publish_time"])

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(url, params=params, data=form)
            print(f"📘 FB post_on_facebook | status={response.status_code} body={response.text[:300]}")
            if response.status_code != HTTPStatus.OK:
                return UriResponse.get_single_data_response(
                    "publish_post", None, code=response.status_code
                )
            return UriResponse.create_response("publish_post", response.json())
        except Exception as e:
            print(f"❌ Error posting to Facebook: {e}")
            raise

    @staticmethod
    async def publish_post(page_id: str, payload: dict, access_token: str):
        """
        Publishes a post to a Facebook Page.
        Sends access_token as query param + form-encoded body (not Bearer + JSON)
        to avoid Facebook error code 1 on certain token types.
        """
        url = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/feed"
        params = {"access_token": access_token}

        published = payload.get("published", True)
        form: Dict[str, Any] = {
            "message": payload.get("message", ""),
            "published": str(published).lower(),
        }
        if payload.get("attached_media"):
            form["attached_media"] = json.dumps(payload["attached_media"])
        if not published and payload.get("scheduled_publish_time"):
            form["scheduled_publish_time"] = str(payload["scheduled_publish_time"])

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, params=params, data=form)

        print(f"📘 FB publish_post | status={response.status_code} body={response.text[:300]}")

        if response.status_code != HTTPStatus.OK:
            return UriResponse.get_single_data_response(
                "publish_post", None, code=response.status_code
            )

        return UriResponse.create_response("publish_post", response.json())
