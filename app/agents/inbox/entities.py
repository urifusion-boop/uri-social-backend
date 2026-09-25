"""
URI Unified Social Inbox — the canonical conversation model.

One inbox across Instagram, Facebook Messenger, WhatsApp and (later) TikTok. The
providers disagree about almost everything — what a thread is, what an id means, when
you may reply — so nothing provider-shaped is allowed past the adapter boundary. These
entities are what the rest of URI sees (PRD §8.2).

Four rules encoded here, each because the obvious shortcut is wrong:

· **A comment is not a DM.** It has a parent comment and an owned post or ad behind it,
  and forcing it into a message shape loses both. `Conversation.kind` keeps them apart.

· **Identities are never merged by guesswork.** The same human on Instagram and
  WhatsApp is two ContactIdentities until something reliable links them, and the link
  lives in its own record so it can be undone without rewriting history. A name and a
  profile picture are not evidence.

· **Provider ids are preserved, always.** Dedup, reconciliation and "did this actually
  send" all depend on being able to ask the provider about the exact object again.

· **Attribution is evidence, not inference.** A conversation carries a campaign id only
  when the provider said so. "I saw your ad" is a sentence, not a link (PRD §8.6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

# Mongo collections. Named so a stray query cannot silently hit another feature's data.
CHANNEL_ACCOUNTS = "inbox_channel_accounts"
IDENTITIES = "inbox_contact_identities"
IDENTITY_LINKS = "inbox_contact_links"
CONVERSATIONS = "inbox_conversations"
MESSAGES = "inbox_messages"
RAW_EVENTS = "inbox_raw_events"
AUDIT = "inbox_audit_events"


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    WHATSAPP = "whatsapp"
    TIKTOK = "tiktok"


class Kind(str, Enum):
    """A DM thread and a comment thread are different objects, not one with a flag."""
    DM = "dm"
    COMMENT = "comment"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Status(str, Enum):
    OPEN = "open"            # work is needed
    PENDING = "pending"      # waiting on the customer or an internal dependency
    RESOLVED = "resolved"    # the agent considers it done


class Delivery(str, Enum):
    """Distinct states, never collapsed. "Sent" must mean the provider accepted it.

    UNKNOWN exists because a timed-out send may well have reached the customer, and
    treating that as failed is how somebody gets messaged twice (PRD §8.4).
    """
    PENDING = "pending"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    UNKNOWN = "unknown"


def now() -> datetime:
    return datetime.now(timezone.utc)


class ContactIdentity(BaseModel):
    """One person ON ONE PLATFORM. Deliberately not "a customer"."""
    workspace_id: str
    platform: Platform
    external_user_id: str                 # provider-scoped; unique per workspace+platform
    display_name: str = ""
    avatar_url: str = ""
    first_seen_at: datetime = Field(default_factory=now)


class Conversation(BaseModel):
    workspace_id: str
    channel_account_id: str
    platform: Platform
    kind: Kind
    # A DM thread id, or the owned post/comment object for a comment thread.
    external_thread_id: str
    contact_identity_id: str = ""
    status: Status = Status.OPEN
    assignee_id: str = ""
    tags: list[str] = Field(default_factory=list)
    last_activity_at: datetime = Field(default_factory=now)
    # When the provider will still accept a free-form reply. None = not known rather
    # than "open forever": the composer must refuse rather than guess (PRD FR04).
    reply_window_expires_at: Optional[datetime] = None
    # Only ever set from provider metadata (PRD §8.6). Never inferred from message text.
    source_post_id: str = ""
    source_ad_id: str = ""
    source_campaign_id: str = ""
    attribution_evidence: str = ""        # e.g. "referral", "comment_on_ad_object"


class Message(BaseModel):
    workspace_id: str
    conversation_id: str
    provider_message_id: str              # unique per account; the dedup key
    direction: Direction
    text: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    parent_id: str = ""                   # a comment's parent comment
    provider_timestamp: Optional[datetime] = None   # when the provider says it happened
    received_at: datetime = Field(default_factory=now)  # when URI saw it
    delivery: Delivery = Delivery.ACCEPTED          # inbound is already a fact
    failure_reason: str = ""
