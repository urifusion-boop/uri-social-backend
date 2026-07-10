"""
Jane + Ads — persisted data model (PRD §B1–B3, §5, split-doc 1.3).

These are the documents stored in Mongo. They are distinct from the in-memory
interface contract in `models.py` (which is what the decision engine and adapters
pass around). Custodial wallet balances live here, ledger-separate from URI
operating cash — a fintech requirement.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .models import CampaignObjective, Goal, Platform, PurchaseBehaviour


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────────────

class TransactionType(str, Enum):
    TOPUP = "topup"                     # customer funds the wallet (credit)
    CONVERSATION_CHARGE = "conversation_charge"   # a delivered CTWA conversation (debit)
    REFUND = "refund"                   # credit back
    ADJUSTMENT = "adjustment"           # manual correction


class TransactionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class WalletStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_FUNDS = "awaiting_funds"
    LIVE = "live"
    PAUSED = "paused"
    COMPLETED = "completed"


# ── Wallet + ledger ───────────────────────────────────────────────────────────

class Wallet(BaseModel):
    """A custodial prepaid Naira balance per business. Held by URI on the client's
    behalf — NOT URI revenue and NOT operating cash."""
    business_id: str
    balance_ngn: float = 0.0
    currency: str = "NGN"
    status: WalletStatus = WalletStatus.ACTIVE
    total_topped_up_ngn: float = 0.0
    total_spent_ngn: float = 0.0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Transaction(BaseModel):
    """One ledger entry. `amount_ngn` is signed: + credit, − debit. `balance_after_ngn`
    snapshots the wallet balance right after this entry, so the ledger is auditable and
    balance == sum(amount_ngn) always holds."""
    transaction_id: str
    business_id: str
    type: TransactionType
    amount_ngn: float                   # signed
    balance_after_ngn: float
    status: TransactionStatus = TransactionStatus.COMPLETED
    reference: str = ""                 # Squad/Paystack ref for top-ups
    campaign_id: str = ""
    ad_id: str = ""
    actual_platform_cost_ngn: Optional[float] = None   # what the platform charged (for pricing)
    created_at: datetime = Field(default_factory=_now)


# ── Other core documents (data model complete) ────────────────────────────────

class Client(BaseModel):
    business_id: str
    name: str = ""
    whatsapp_number: str = ""
    category: str = ""
    connected_page_id: str = ""         # set once the FB page is connected (Ibukun's flow)
    created_at: datetime = Field(default_factory=_now)


class Campaign(BaseModel):
    """A campaign carries its OWN goal, behaviour, and platform decision — the
    architectural consequence of 'decide per campaign, not per business' (PRD §5)."""
    campaign_id: str
    business_id: str
    goal: Goal
    behaviour: PurchaseBehaviour
    objective: CampaignObjective = CampaignObjective.CONVERSATIONS
    status: CampaignStatus = CampaignStatus.DRAFT
    platform_campaign_ids: dict[str, str] = Field(default_factory=dict)  # platform → external id
    per_business_cap_ngn: float = 0.0
    explanation: str = ""
    created_at: datetime = Field(default_factory=_now)


class Ad(BaseModel):
    """One business's ad inside a (possibly pooled) ad set — own creative + own WhatsApp
    number, tracked separately (PRD §B4)."""
    ad_id: str
    campaign_id: str
    business_id: str
    platform: Platform
    creative_url: str = ""
    whatsapp_number: str = ""
    external_ad_id: str = ""            # the platform's id, filled by the adapter
    spend_ngn: float = 0.0
    conversations: int = 0


class Conversation(BaseModel):
    conversation_id: str
    business_id: str
    ad_id: str
    campaign_id: str
    platform: Platform
    charged_ngn: float
    actual_platform_cost_ngn: Optional[float] = None
    at: datetime = Field(default_factory=_now)


class AuthorizationRecord(BaseModel):
    """Audit trail of the client's explicit grant (PRD Part D): run ads on their behalf,
    spend from their wallet, represent their business. Who / what / when."""
    business_id: str
    authorized_run_ads: bool = False
    authorized_spend_wallet: bool = False
    authorized_represent: bool = False
    granted_by: str = ""                # user id/email
    at: datetime = Field(default_factory=_now)
