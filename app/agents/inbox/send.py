"""
Unified Inbox — replying (PRD §8.4).

Sending is the half that can hurt a customer. Ingestion getting something wrong shows
an agent a bad row; sending getting something wrong messages a real person, twice, or
at a moment the platform forbids. Four rules, each the opposite of the shortcut:

· **Record the intent BEFORE calling the provider.** A send that times out may well
  have arrived. If the outbox row is written after the call, that message is invisible
  and the natural fix — retry — messages the customer again.

· **A timeout is UNKNOWN, never FAILED.** FAILED invites a retry. UNKNOWN says a human
  must look, which is the honest state when the provider never answered.

· **Idempotency is keyed by the caller.** A double-clicked send button and a retried
  HTTP request are indistinguishable from here, so the client supplies a key and the
  second call returns the first result instead of sending again.

· **Eligibility is checked against a recorded window, not a guess.** Outside Meta's
  24-hour window a free-form reply is refused up front, with a reason the composer can
  show, rather than sent and rejected by the provider in front of the customer.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from bson import ObjectId

from .entities import (
    AUDIT, CONVERSATIONS, MESSAGES, OUTBOX, Delivery, Direction, Kind, now,
)


class SendRefused(Exception):
    """Refused before the provider was called. Carries a reason the UI can show."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class ProviderTimeout(Exception):
    """The provider never answered. The message may or may not have been delivered."""


# A transport is injected so the decision logic here is testable without Meta, and so
# each platform's quirks stay behind one boundary.
Transport = Callable[[dict, dict, str], Awaitable[str]]


def reply_window_open(conv: dict, at=None) -> bool:
    """Meta allows a free-form reply for 24 hours after the customer's last message.

    An unset expiry means NOT KNOWN, and not-known refuses. Treating it as open is how
    you send into a closed window and get an error in front of the customer.
    """
    expires = conv.get("reply_window_expires_at")
    if not expires:
        return False
    return expires > (at or now())


def check_eligibility(conv: dict, at=None) -> None:
    """Raise SendRefused if this conversation cannot be replied to right now."""
    if conv.get("kind") == Kind.COMMENT.value:
        return                      # public comment threads have no 24-hour window

    if not reply_window_open(conv, at):
        raise SendRefused(
            "window_closed",
            "The 24-hour reply window has closed. Meta only allows a free-form reply "
            "within 24 hours of the customer's last message.",
        )


async def _existing_by_key(db, workspace_id: str, key: str) -> Optional[dict]:
    if not key:
        return None
    return await db[OUTBOX].find_one({"workspace_id": workspace_id, "idempotency_key": key})


async def send_reply(
    db,
    workspace_id: str,
    conversation_id: str,
    text: str,
    idempotency_key: str,
    actor_id: str,
    transport: Transport,
    account: Optional[dict] = None,
) -> dict:
    """Send one reply. Returns the outbox record.

    Never raises for a provider failure — a failure is a recorded state, because the
    agent needs to see what happened to their message, not an exception.
    """
    text = (text or "").strip()
    if not text:
        raise SendRefused("empty", "A reply cannot be empty.")

    # Idempotency first: before any lookup, so a retry is cheap and, more importantly,
    # cannot take a different path than the original.
    prior = await _existing_by_key(db, workspace_id, idempotency_key)
    if prior:
        return prior

    if not ObjectId.is_valid(conversation_id):
        raise SendRefused("not_found", "No such conversation.")

    conv = await db[CONVERSATIONS].find_one(
        {"_id": ObjectId(conversation_id), "workspace_id": workspace_id})
    if not conv:
        # Scoped by workspace, so another tenant's id is indistinguishable from a
        # wrong one. That is the point.
        raise SendRefused("not_found", "No such conversation.")

    check_eligibility(conv)

    # A comment reply goes to the specific comment, not the post. Resolved here so the
    # transport stays a thin provider call with no queries of its own.
    if conv.get("kind") == Kind.COMMENT.value:
        latest = await db[MESSAGES].find(
            {"workspace_id": workspace_id, "conversation_id": conversation_id,
             "direction": Direction.INBOUND.value},
        ).sort("received_at", -1).limit(1).to_list(1)
        if not latest:
            raise SendRefused("no_target", "Nothing to reply to in this thread yet.")
        conv = {**conv, "_reply_to": latest[0].get("provider_message_id", "")}

    outbox = {
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "idempotency_key": idempotency_key,
        "actor_id": actor_id,
        "text": text,
        "delivery": Delivery.PENDING.value,
        "provider_message_id": "",
        "failure_reason": "",
        "created_at": now(),
    }
    res = await db[OUTBOX].insert_one(dict(outbox))
    outbox_id = res.inserted_id

    async def _finish(delivery: str, provider_message_id: str = "", failure: str = ""):
        await db[OUTBOX].update_one(
            {"_id": outbox_id},
            {"$set": {"delivery": delivery,
                      "provider_message_id": provider_message_id,
                      "failure_reason": failure,
                      "completed_at": now()}},
        )
        await db[AUDIT].insert_one({
            "workspace_id": workspace_id, "actor_id": actor_id,
            "action": "inbox.reply", "conversation_id": conversation_id,
            "delivery": delivery, "at": now(),
        })
        return {**outbox, "_id": outbox_id, "delivery": delivery,
                "provider_message_id": provider_message_id, "failure_reason": failure}

    try:
        provider_message_id = await transport(conv, account or {}, text)
    except ProviderTimeout as e:
        # NOT failed. It may have arrived; a retry could double-send.
        return await _finish(Delivery.UNKNOWN.value, failure=str(e) or "provider timeout")
    except Exception as e:
        return await _finish(Delivery.FAILED.value, failure=str(e)[:500])

    if not provider_message_id:
        # The provider answered without an id, so there is nothing to reconcile against
        # later. Not a success.
        return await _finish(Delivery.UNKNOWN.value,
                             failure="provider returned no message id")

    await db[MESSAGES].insert_one({
        "workspace_id": workspace_id,
        "conversation_id": conversation_id,
        "provider_message_id": provider_message_id,
        "direction": Direction.OUTBOUND.value,
        "text": text,
        "attachments": [],
        "parent_id": "",
        "provider_timestamp": now(),
        "received_at": now(),
        "delivery": Delivery.ACCEPTED.value,
        "actor_id": actor_id,
    })
    await db[CONVERSATIONS].update_one(
        {"_id": conv["_id"]}, {"$set": {"last_activity_at": now()}})

    return await _finish(Delivery.ACCEPTED.value, provider_message_id)
