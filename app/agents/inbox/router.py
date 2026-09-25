"""
Unified Inbox — the webhook Meta calls, and the endpoints the inbox screen reads.

The webhook has no JWT: Meta calls it directly, so the signature is the only thing
separating a real customer message from anything a stranger POSTs at the URL.
"""
from __future__ import annotations

import json
from typing import Optional

from bson import ObjectId

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.dependencies import get_active_brand_context, get_db_dependency

from .entities import CHANNEL_ACCOUNTS, CONVERSATIONS, IDENTITIES, MESSAGES
from .ingest import (
    ensure_indexes, parse_meta_event, record_event, store_raw, verify_signature,
)

router = APIRouter(prefix="/inbox", tags=["Unified Inbox"])


@router.get("/webhooks/meta")
async def verify_meta_webhook(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_challenge: str = Query("", alias="hub.challenge"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
) -> Response:
    """Meta's subscription handshake — it GETs this once and expects the challenge back.

    Returns the challenge as PLAIN TEXT. Meta compares the body byte for byte, so a
    JSON-wrapped or quoted response fails the handshake with no useful error.
    """
    expected = getattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "") or ""
    if hub_mode == "subscribe" and expected and hub_verify_token == expected:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/webhooks/meta")
async def receive_meta_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Instagram, Messenger and WhatsApp events land here.

    Acknowledge quickly and process after: Meta retries anything slow or non-200, and
    a retry storm turns one customer message into several. The raw event is stored
    first so a normalisation bug can be replayed rather than losing the message.
    """
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    if not verify_signature(raw_body, signature, settings.META_APP_SECRET):
        print("[Inbox] rejected a webhook with a missing/invalid signature", flush=True)
        raise HTTPException(status_code=401, detail="invalid signature")

    await store_raw(db, "meta", raw_body, dict(request.headers))

    try:
        payload = json.loads(raw_body)
    except ValueError:
        return {"status": "ignored", "reason": "unparseable body"}

    events = parse_meta_event(payload)
    stored = 0
    for ev in events:
        account = await db[CHANNEL_ACCOUNTS].find_one(
            {"external_account_id": ev.get("external_account_id")})
        if not account:
            # An event for an account nobody connected. Dropping it is right: there is
            # no workspace to file it under, and guessing one would put a stranger's
            # message in somebody's inbox.
            print(f"[Inbox] event for unconnected account {ev.get('external_account_id')}", flush=True)
            continue
        msg_id = await record_event(
            db, account["workspace_id"], str(account["_id"]),
            account.get("platform") or "facebook", ev)
        if msg_id:
            stored += 1
    return {"status": "ok", "events": len(events), "stored": stored}


@router.get("/conversations")
async def list_conversations(
    channel: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    limit: int = 50,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The inbox list. Scoped to the caller's workspace, always."""
    q: dict = {"workspace_id": brand_ctx.get("brand_id")}
    if channel:
        q["platform"] = channel
    if status:
        q["status"] = status
    if assignee:
        q["assignee_id"] = assignee

    rows = await db[CONVERSATIONS].find(q).sort("last_activity_at", -1).to_list(min(limit, 200))

    # Identities and last messages in two queries, not two per row: an inbox list of
    # 200 threads would otherwise be 400 round trips.
    ids = {r["contact_identity_id"] for r in rows if r.get("contact_identity_id")}
    names: dict[str, str] = {}
    if ids:
        # Stored as a string, so it has to be converted back to query _id.
        oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
        async for ident in db[IDENTITIES].find({"_id": {"$in": oids}}):
            names[str(ident["_id"])] = ident.get("display_name") or ""

    out = []
    for r in rows:
        last = await db[MESSAGES].find({"conversation_id": str(r["_id"])}) \
            .sort("received_at", -1).limit(1).to_list(1)
        out.append({
            "id": str(r["_id"]),
            "platform": r.get("platform"),
            "kind": r.get("kind"),
            "status": r.get("status"),
            "assignee_id": r.get("assignee_id", ""),
            "tags": r.get("tags", []),
            "last_activity_at": r.get("last_activity_at"),
            "contact_name": names.get(r.get("contact_identity_id", ""), ""),
            "excerpt": (last[0].get("text") if last else "")[:140],
            # Present only when the provider supplied it (PRD §8.6).
            "source_post_id": r.get("source_post_id", ""),
            "source_ad_id": r.get("source_ad_id", ""),
        })
    return {"conversations": out}


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """One thread. The workspace filter is on the QUERY, not checked afterwards —
    a wrong id must return nothing rather than reveal that it exists."""
    rows = await db[MESSAGES].find({
        "workspace_id": brand_ctx.get("brand_id"),
        "conversation_id": conversation_id,
    }).sort("received_at", 1).to_list(500)
    return {"messages": [{
        "id": str(r["_id"]),
        "direction": r.get("direction"),
        "text": r.get("text", ""),
        "attachments": r.get("attachments", []),
        "delivery": r.get("delivery"),
        "provider_timestamp": r.get("provider_timestamp"),
        "received_at": r.get("received_at"),
    } for r in rows]}


@router.post("/admin/ensure-indexes")
async def create_indexes(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The uniqueness that makes Meta's redelivery harmless. Safe to re-run."""
    await ensure_indexes(db)
    return {"status": "ok"}
