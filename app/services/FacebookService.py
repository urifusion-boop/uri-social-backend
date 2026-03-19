"""
Minimal FacebookService stub for the uri-agent.
Only implements the two methods used by ApprovalWorkflowService.
"""
import requests
from http import HTTPStatus
from typing import Any, Dict

from app.core.config import settings
from app.domain.responses.uri_response import UriResponse


class FacebookService:
    @staticmethod
    async def post_on_facebook(page_id: str, post_data: dict, access_token: str):
        """
        Posts content on Facebook using the Graph API.
        """
        url = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/feed"
        headers = {"Authorization": f"Bearer {access_token}"}

        # Build payload from post_data dict
        payload: Dict[str, Any] = {
            "message": post_data.get("message", ""),
            "published": post_data.get("published", True),
        }
        if post_data.get("attached_media"):
            payload["attached_media"] = post_data["attached_media"]
        if post_data.get("scheduled_publish_time"):
            payload["scheduled_publish_time"] = post_data["scheduled_publish_time"]

        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code != HTTPStatus.OK:
                return UriResponse.get_single_data_response(
                    "publish_post", None, code=response.status_code
                )
            return UriResponse.create_response("publish_post", response.json())
        except Exception as e:
            print(f"Error posting to Facebook: {e}")
            raise

    @staticmethod
    async def publish_post(page_id: str, payload: dict, access_token: str):
        """
        Publishes a post to a Facebook Page.
        """
        url = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/feed"
        headers = {"Authorization": f"Bearer {access_token}"}

        if payload.get("published"):
            payload.pop("scheduled_publish_time", None)

        response = requests.post(url, headers=headers, json=payload)

        if response.status_code != HTTPStatus.OK:
            return UriResponse.get_single_data_response(
                "publish_post", None, code=response.status_code
            )

        return UriResponse.create_response("publish_post", response.json())
