"""Outbound client for jane-whatsapp-reply's authenticated /internal/* API — the
two things that service must keep sole ownership of: decrypted message bodies
(only jane holds FIELD_ENCRYPTION_KEY/PHONE_ENCRYPTION_KEY) and the actual
WhatsApp send (only jane holds WHATSAPP_ACCESS_TOKEN). Everything else about an
escalated conversation (list/filter view, state) is read directly from
jane_wa_conversations on the shared Mongo cluster — see jane_escalations_router.py.

Unlike UriGatewayService's pattern (swallow failures, return None), errors here
are allowed to propagate as httpx exceptions — a failed reply-send or resolve
must be visible to the calling support agent, not silently absorbed into a None
the frontend can't distinguish from "nothing to report."
"""

from typing import Any, Dict, List

import httpx

from app.core.config import settings


class JaneEscalationClient:
    base_url = settings.JANE_WA_BASE_URL

    @staticmethod
    def _headers(agent_email: str, agent_id: str) -> Dict[str, str]:
        return {
            "X-Internal-Service": settings.JANE_WA_INTERNAL_SECRET,
            "X-Agent-Email": agent_email,
            "X-Agent-Id": agent_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    async def get_conversation_messages(conversation_id: str, agent_email: str, agent_id: str) -> List[Dict[str, Any]]:
        url = f"{JaneEscalationClient.base_url}/internal/conversations/{conversation_id}/messages"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=JaneEscalationClient._headers(agent_email, agent_id))
            response.raise_for_status()
            return response.json()

    @staticmethod
    async def send_agent_reply(
        conversation_id: str,
        text: str,
        agent_email: str,
        agent_id: str,
        idempotency_key: str,
        channel: str = "dashboard",
    ) -> Dict[str, Any]:
        url = f"{JaneEscalationClient.base_url}/internal/conversations/{conversation_id}/reply"
        body = {"text": text, "idempotency_key": idempotency_key, "channel": channel}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=body, headers=JaneEscalationClient._headers(agent_email, agent_id))
            response.raise_for_status()
            return response.json()

    @staticmethod
    async def resolve_conversation(conversation_id: str, agent_email: str, agent_id: str) -> Dict[str, Any]:
        url = f"{JaneEscalationClient.base_url}/internal/conversations/{conversation_id}/resolve"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json={}, headers=JaneEscalationClient._headers(agent_email, agent_id))
            response.raise_for_status()
            return response.json()
