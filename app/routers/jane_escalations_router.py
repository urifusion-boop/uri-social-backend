"""
Customer-care support endpoints for jane-whatsapp-reply's escalated conversations.

Hybrid architecture (see the approved plan): reads jane_wa_conversations DIRECTLY
from the shared MongoDB Atlas cluster (no encrypted fields, cheap/safe — same
cross-service-read precedent as brand_facts_reader.py/client_registry.py in
jane-whatsapp-reply itself, just the other direction). Proxies to
jane-whatsapp-reply's own /internal/* API, authenticated by JANE_WA_INTERNAL_SECRET,
for exactly the two things that service must keep sole ownership of: decrypted
message bodies and the actual WhatsApp send — see JaneEscalationClient.py.
"""

from typing import Any, Dict, List, Optional

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.database import get_db
from app.routers.admin_router import verify_support
from app.services.JaneEscalationClient import JaneEscalationClient

router = APIRouter(prefix="/api/support/escalations", tags=["Support"])

CONVERSATIONS_COLLECTION = "jane_wa_conversations"


def _agent_identity(support_user: Dict[str, Any]) -> tuple:
    claims = support_user.get("claims", {}) or {}
    agent_email = claims.get("email", "")
    agent_id = claims.get("userId", "")
    if not agent_email or not agent_id:
        raise HTTPException(status_code=401, detail="Invalid token: missing agent identity")
    return agent_email, agent_id


def _parse_object_id(conversation_id: str) -> ObjectId:
    try:
        return ObjectId(conversation_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid conversation id")


def _serialize_conversation(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "brand_id": doc.get("brand_id"),
        "state": doc.get("state"),
        "escalated_reason": doc.get("escalated_reason"),
        "escalated_at": doc.get("escalated_at"),
        "last_message_at": doc.get("last_message_at"),
        "created_at": doc.get("created_at"),
        "resolved_via": doc.get("resolved_via"),
        "resolved_by": doc.get("resolved_by"),
        # Deliberately no phone number of any form here — see
        # jane-whatsapp-reply's crypto_utils.py on why phone_encrypted's decrypt
        # access is narrowed to exactly two call sites, neither of which is a
        # list/read API like this one.
    }


async def _forward_jane_error(exc: httpx.HTTPStatusError):
    """jane-whatsapp-reply's own 404/409/401 responses carry the real reason
    (conversation not found, not escalated, reply already in flight) — forward
    the status and detail rather than collapsing everything into a generic 502."""
    try:
        detail = exc.response.json().get("detail", exc.response.text)
    except Exception:
        detail = exc.response.text
    raise HTTPException(status_code=exc.response.status_code, detail=detail)


@router.get("")
async def list_escalations(
    state: Optional[str] = Query(None, description="Filter by conversation state, e.g. 'escalated'"),
    brand_id: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _support_user: Dict[str, Any] = Depends(verify_support),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if state:
        query["state"] = state
    if brand_id:
        query["brand_id"] = brand_id

    total = await db[CONVERSATIONS_COLLECTION].count_documents(query)
    skip = (page - 1) * limit

    conversations: List[Dict[str, Any]] = []
    cursor = db[CONVERSATIONS_COLLECTION].find(query).sort("last_message_at", -1).skip(skip).limit(limit)
    async for doc in cursor:
        conversations.append(_serialize_conversation(doc))

    return {
        "conversations": conversations,
        "pagination": {
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit,
        },
    }


@router.get("/{conversation_id}")
async def get_escalation_detail(
    conversation_id: str,
    support_user: Dict[str, Any] = Depends(verify_support),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Dict[str, Any]:
    object_id = _parse_object_id(conversation_id)
    doc = await db[CONVERSATIONS_COLLECTION].find_one({"_id": object_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Conversation not found")

    agent_email, agent_id = _agent_identity(support_user)
    try:
        messages = await JaneEscalationClient.get_conversation_messages(conversation_id, agent_email, agent_id)
    except httpx.HTTPStatusError as exc:
        await _forward_jane_error(exc)

    return {**_serialize_conversation(doc), "messages": messages}


class ReplyRequest(BaseModel):
    text: str
    idempotency_key: str


@router.post("/{conversation_id}/reply")
async def reply_to_escalation(
    conversation_id: str,
    body: ReplyRequest,
    support_user: Dict[str, Any] = Depends(verify_support),
) -> Dict[str, Any]:
    _parse_object_id(conversation_id)  # validate shape before round-tripping to jane
    agent_email, agent_id = _agent_identity(support_user)
    try:
        return await JaneEscalationClient.send_agent_reply(
            conversation_id, body.text, agent_email, agent_id, body.idempotency_key, channel="dashboard"
        )
    except httpx.HTTPStatusError as exc:
        await _forward_jane_error(exc)


@router.post("/{conversation_id}/resolve")
async def resolve_escalation(
    conversation_id: str,
    support_user: Dict[str, Any] = Depends(verify_support),
) -> Dict[str, Any]:
    _parse_object_id(conversation_id)
    agent_email, agent_id = _agent_identity(support_user)
    try:
        return await JaneEscalationClient.resolve_conversation(conversation_id, agent_email, agent_id)
    except httpx.HTTPStatusError as exc:
        await _forward_jane_error(exc)
