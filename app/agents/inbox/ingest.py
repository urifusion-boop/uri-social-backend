"""
Unified Inbox — taking events from Meta and turning them into conversations.

The security boundary of the whole feature. This endpoint has no JWT: Meta calls it
directly, so the signature is the ONLY thing distinguishing a real event from anyone
who guessed the URL. Without it, a stranger could POST a fabricated customer message
into a business's inbox and an agent would answer it (PRD §8.3).

Three rules, each learned from how these go wrong:

· **Verify against the RAW body.** Re-serialising JSON changes bytes — key order,
  spacing, unicode escapes — and the HMAC then never matches. The raw bytes are what
  Meta signed.

· **Acknowledge fast, process after.** Meta retries anything slow or non-200, which
  turns one event into several. The raw event is stored and a 200 returned before any
  normalisation work.

· **Deduplicate on the provider's own id.** At-least-once delivery is the contract, so
  the same message WILL arrive twice. A unique index on (account, provider_message_id)
  makes the second arrival a no-op rather than a duplicate in the agent's list.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Optional

from .entities import (
    AUDIT, CONVERSATIONS, IDENTITIES, MESSAGES, RAW_EVENTS,
    Conversation, ContactIdentity, Delivery, Direction, Kind, Message, Platform, now,
)


class SignatureError(Exception):
    """The request did not come from Meta, or was tampered with in flight."""


def verify_signature(raw_body: bytes, header: str, app_secret: str) -> bool:
    """Meta's X-Hub-Signature-256 over the raw request body.

    compare_digest, not ==: a plain comparison leaks how much of the signature matched
    through its timing, which is enough to forge one given enough attempts.
    """
    if not header or not app_secret:
        return False
    prefix, _, sent = header.partition("=")
    if prefix != "sha256" or not sent:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


async def ensure_indexes(db) -> None:
    """The uniqueness that makes redelivery harmless.

    Enforced by the DATABASE, not by a check-then-insert in application code: two
    workers processing the same retry would both find nothing and both insert.
    """
    await db[MESSAGES].create_index(
        [("workspace_id", 1), ("provider_message_id", 1)], unique=True, name="uniq_provider_message")
    await db[IDENTITIES].create_index(
        [("workspace_id", 1), ("platform", 1), ("external_user_id", 1)],
        unique=True, name="uniq_identity")
    await db[CONVERSATIONS].create_index(
        [("workspace_id", 1), ("platform", 1), ("kind", 1), ("external_thread_id", 1)],
        unique=True, name="uniq_thread")
    await db[CONVERSATIONS].create_index([("workspace_id", 1), ("last_activity_at", -1)])
    await db[RAW_EVENTS].create_index([("received_at", -1)])


async def store_raw(db, platform: str, raw_body: bytes, headers: dict) -> str:
    """Persist what arrived, before interpreting any of it.

    Kept so a normalisation bug can be replayed rather than losing the customer's
    message, and so reconciliation has something to compare against (PRD §8.3).
    """
    doc = {
        "platform": platform,
        "body": raw_body.decode("utf-8", errors="replace"),
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "headers": {k: v for k, v in headers.items() if k.lower().startswith("x-hub")},
        "received_at": now(),
        "processed": False,
    }
    res = await db[RAW_EVENTS].insert_one(doc)
    return str(res.inserted_id)


def _ts(value: Any) -> Optional[datetime]:
    """Meta sends epoch seconds in some payloads and milliseconds in others."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 1e11:          # milliseconds
        n /= 1000
    return datetime.fromtimestamp(n, timezone.utc)


def parse_meta_event(payload: dict) -> list[dict]:
    """Flatten a Meta webhook into normalised events.

    One POST can carry several entries, each with several changes, across pages — so
    this returns a list. Anything unrecognised is skipped rather than guessed at: a
    half-understood event in an agent's inbox is worse than a gap the health check
    reports.
    """
    events: list[dict] = []
    for entry in payload.get("entry") or []:
        account_id = str(entry.get("id") or "")

        # Messenger / Instagram DMs
        for m in entry.get("messaging") or []:
            msg = m.get("message") or {}
            if not msg or msg.get("is_echo"):
                continue      # echoes are our own sends coming back
            events.append({
                "type": "dm",
                "external_account_id": account_id,
                "external_user_id": str((m.get("sender") or {}).get("id") or ""),
                "external_thread_id": str((m.get("sender") or {}).get("id") or ""),
                "provider_message_id": str(msg.get("mid") or ""),
                "text": msg.get("text") or "",
                "attachments": msg.get("attachments") or [],
                "provider_timestamp": _ts(m.get("timestamp")),
            })

        # Comments on owned posts and ads
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            if ch.get("field") not in ("comments", "feed"):
                continue
            if v.get("item") not in (None, "comment"):
                continue
            comment_id = str(v.get("comment_id") or v.get("id") or "")
            if not comment_id:
                continue
            frm = v.get("from") or {}
            events.append({
                "type": "comment",
                "external_account_id": account_id,
                "external_user_id": str(frm.get("id") or ""),
                "display_name": frm.get("name") or "",
                # The post is the thread: every comment on it belongs together.
                "external_thread_id": str(v.get("post_id") or v.get("media") or comment_id),
                "provider_message_id": comment_id,
                "parent_id": str((v.get("parent_id") or "")),
                "text": v.get("message") or v.get("text") or "",
                "provider_timestamp": _ts(v.get("created_time")),
                # Provider-supplied only. Never derived from the comment's wording.
                "source_post_id": str(v.get("post_id") or ""),
                "source_ad_id": str(v.get("ad_id") or ""),
            })
    return events


async def _upsert_identity(db, workspace_id: str, platform: str, ev: dict) -> str:
    """The person, on this platform only.

    Never looks for a matching person on another platform: two identities that happen
    to share a display name are not evidence of the same human, and a wrong merge shows
    one customer another customer's conversation.
    """
    key = {"workspace_id": workspace_id, "platform": platform,
           "external_user_id": ev.get("external_user_id") or ""}
    update: dict[str, Any] = {"$setOnInsert": {**key, "first_seen_at": now()}}
    # Only when there is something to set: Mongo rejects an empty $set outright, and
    # DM webhooks carry no display name at all, so this is the common path.
    if ev.get("display_name"):
        update["$set"] = {"display_name": ev["display_name"]}

    doc = await db[IDENTITIES].find_one_and_update(
        key, update, upsert=True, return_document=True,
    )
    return str(doc["_id"])


async def _upsert_conversation(db, workspace_id: str, channel_account_id: str,
                               platform: str, ev: dict, identity_id: str) -> str:
    kind = Kind.COMMENT.value if ev["type"] == "comment" else Kind.DM.value
    key = {"workspace_id": workspace_id, "platform": platform, "kind": kind,
           "external_thread_id": ev.get("external_thread_id") or ""}
    on_insert = {
        **key, "channel_account_id": channel_account_id,
        "contact_identity_id": identity_id, "status": "open",
        "assignee_id": "", "tags": [],
    }
    # Attribution is written ONCE, on insert, and only from what the provider sent.
    for field in ("source_post_id", "source_ad_id"):
        if ev.get(field):
            on_insert[field] = ev[field]
            on_insert["attribution_evidence"] = "provider_webhook"

    doc = await db[CONVERSATIONS].find_one_and_update(
        key,
        {"$setOnInsert": on_insert,
         # New activity on a resolved thread REOPENS it (PRD §4.2) — a customer writing
         # again is unambiguously work, whatever an agent concluded earlier.
         "$set": {"last_activity_at": ev.get("provider_timestamp") or now()}},
        upsert=True, return_document=True,
    )
    if doc.get("status") == "resolved":
        await db[CONVERSATIONS].update_one({"_id": doc["_id"]}, {"$set": {"status": "open"}})
    return str(doc["_id"])


async def record_event(db, workspace_id: str, channel_account_id: str,
                       platform: str, ev: dict) -> Optional[str]:
    """Store one normalised event. Returns the message id, or None if already seen.

    Idempotent by the provider's own message id: Meta's delivery guarantee is
    at-least-once, so this WILL be called twice for the same message and the second
    call must change nothing.
    """
    if not ev.get("provider_message_id"):
        return None      # nothing to deduplicate on; safer to drop than to duplicate

    existing = await db[MESSAGES].find_one(
        {"workspace_id": workspace_id, "provider_message_id": ev["provider_message_id"]},
        {"_id": 1},
    )
    if existing:
        return None

    identity_id = await _upsert_identity(db, workspace_id, platform, ev)
    conversation_id = await _upsert_conversation(
        db, workspace_id, channel_account_id, platform, ev, identity_id)

    msg = {
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "provider_message_id": ev["provider_message_id"],
        "direction": Direction.INBOUND.value,
        "text": ev.get("text") or "",
        "attachments": ev.get("attachments") or [],
        "parent_id": ev.get("parent_id") or "",
        "provider_timestamp": ev.get("provider_timestamp"),
        "received_at": now(),
        "delivery": Delivery.ACCEPTED.value,
    }
    try:
        res = await db[MESSAGES].insert_one(msg)
    except Exception as e:
        # The unique index firing IS the dedup working — a concurrent retry got here
        # first. Not an error worth surfacing.
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            return None
        raise
    return str(res.inserted_id)
