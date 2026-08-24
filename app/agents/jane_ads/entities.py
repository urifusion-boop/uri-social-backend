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

from pydantic import BaseModel, Field, field_validator, model_validator

from .models import CampaignObjective, Goal, Platform, PurchaseBehaviour


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────────────

class TransactionType(str, Enum):
    TOPUP = "topup"                     # customer funds the wallet (credit)
    AD_SPEND = "ad_spend"               # recoup real Meta ad spend × markup (debit) — the
                                        # production billing meter, see billing.py
    CONVERSATION_CHARGE = "conversation_charge"   # a delivered CTWA conversation (debit) —
                                        # legacy per-conversation meter, kept for the /plan demo
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


# ── Ad strategy corpus (Jane Ads Playbook v1) ────────────────────────────────
# The cold-start library Jane retrieves from when building a campaign plan. Seeded
# by hand (URI_Ad_Strategy_Corpus_Seed_v1.xlsx) to set the schema and quality bar
# that later automated ingestion is measured against.
#
# Enum values are the literal strings from the workbook's Lists tab — the sheet is
# the source of truth and exports straight in, so these must match character for
# character or ingestion silently drops rows.

class StrategyCategory(str, Enum):
    OFFER_POSITIONING = "Offer & Positioning"
    AUDIENCE_CONSTRUCTION = "Audience Construction"
    CREATIVE_FORMATS = "Creative Formats & Hooks"
    COPY_ANGLES = "Copy Angles"
    BUDGET_PACING = "Budget, Pacing & Timing"
    MICRO_BUDGET_TESTING = "Micro-Budget Testing"
    CONVERSION_MECHANICS = "Conversion Mechanics"
    RETARGETING = "Retargeting & Sequencing"
    PLATFORM_MECHANICS = "Platform Mechanics"
    DIAGNOSTICS = "Diagnostics & Troubleshooting"


class StrategyPlatform(str, Enum):
    META = "Meta (FB/IG)"
    TIKTOK = "TikTok"
    GOOGLE = "Google"
    LINKEDIN = "LinkedIn"
    SNAPCHAT = "Snapchat"
    WHATSAPP = "WhatsApp"
    CROSS_PLATFORM = "Cross-platform"


class FunnelStage(str, Enum):
    AWARENESS = "Awareness"
    CONSIDERATION = "Consideration"
    CONVERSION = "Conversion"
    RETENTION = "Retention"
    FULL_FUNNEL = "Full funnel"


class SalesCycle(str, Enum):
    SAME_DAY = "Same day"
    ONE_TO_SEVEN_DAYS = "1-7 days"
    ONE_TO_FOUR_WEEKS = "1-4 weeks"
    OVER_A_MONTH = "Over a month"
    NOT_APPLICABLE = "Not applicable"


class EvidenceGrade(str, Enum):
    """Ordered worst-to-best by `rank` below — retrieval prefers A over C when both fit."""
    A_VERIFIED = "A - Verified case study with numbers"
    B_PRACTITIONER = "B - Practitioner anecdote"
    C_GURU = "C - Guru assertion"
    D_UNSUPPORTED = "D - Unsupported"

    @property
    def rank(self) -> int:
        return {"A": 4, "B": 3, "C": 2, "D": 1}[self.value[0]]


class MarketOrigin(str, Enum):
    NIGERIA = "Nigeria"
    AFRICA_OTHER = "Africa (other)"
    US = "US"
    UK_EU = "UK/EU"
    ASIA = "Asia"
    GLOBAL_UNSPECIFIED = "Global/Unspecified"
    LATIN_AMERICA = "Latin America"
    NIGERIA_DESK_RESEARCH = "Nigeria (desk research)"


class TransferVerdict(str, Enum):
    """Whether a tactic sourced elsewhere survives contact with our market.
    DOES_NOT_TRANSFER records are kept deliberately — they stop Jane rediscovering
    a dead tactic later, so they are retrievable, never filtered out at ingestion."""
    AS_IS = "Applies as-is"
    WITH_MODIFICATION = "Applies with modification"
    DOES_NOT_TRANSFER = "Does not transfer"


class SourceType(str, Enum):
    OWN_ACCOUNT_DATA = "Own account data"
    INTERNAL_TEAM = "Internal team knowledge"
    REDDIT = "Reddit thread"
    YOUTUBE = "YouTube video"
    INSTAGRAM = "Instagram video"
    BLOG_ARTICLE = "Blog/article"
    PLATFORM_DOCS = "Platform documentation"
    CASE_STUDY = "Case study"
    BOOK_COURSE = "Book/course"


class StrategyStatus(str, Enum):
    DRAFT = "Draft"
    IN_REVIEW = "In review"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    EXAMPLE = "EXAMPLE"           # the four pre-filled EX-* rows; never ingested


class LocalTestStatus(str, Enum):
    NOT_YET_TESTED = "Not yet tested"
    TESTING = "Testing"
    CONFIRMED_LOCALLY = "Confirmed locally"
    UNDERPERFORMED_LOCALLY = "Underperformed locally"
    RETIRED = "Retired"


class Strategy(BaseModel):
    """One tactic. Never a bundle — a source with six ideas is six Strategy records,
    because a bundle is unusable at retrieval.

    The workbook's hard rule is that a row is only a record if every mandatory
    (pink) field is filled; anything less is a note and must not enter the corpus.
    That rule is enforced here rather than left to the importer, so no path into
    Mongo can bypass it.
    """

    # ── Mandatory (pink columns A–M). `strategy_id` is assigned, not authored,
    # which is why the workbook counts twelve authored fields across thirteen columns.
    strategy_id: str                       # A  ID           (EX-01, SEED-001, …)
    category: StrategyCategory             # B  Category
    claim: str                             # C  Claim
    business_type: str                     # D  Business Type
    budget_floor_ngn_per_day: Optional[float]  # E  Budget Floor (₦/day) — see validator
    platform: StrategyPlatform             # F  Platform
    funnel_stage: FunnelStage              # G  Funnel Stage
    product_price_band_ngn: str            # H  Product Price Band (₦) — free text ("Any",
                                           #    "3,000 - 100,000"); not a Lists dropdown
    sales_cycle: SalesCycle                # I  Sales Cycle
    mechanism: str                         # J  Mechanism — why it works
    evidence_grade: EvidenceGrade          # K  Evidence Grade
    market_origin: MarketOrigin            # L  Market Origin
    transfer_verdict: TransferVerdict      # M  Transfer Verdict

    # ── Optional (grey columns N–W): provenance and local-validation trail.
    modification_required: Optional[str] = None      # N
    source_type: Optional[SourceType] = None         # O
    source_link: Optional[str] = None                # P
    source_date: Optional[datetime] = None           # Q
    seeded_by: Optional[str] = None                  # R
    date_added: Optional[datetime] = None            # S
    status: Optional[StrategyStatus] = None          # T
    local_test_status: Optional[LocalTestStatus] = None   # U
    local_result_notes: Optional[str] = None         # V
    last_reviewed: Optional[datetime] = None         # W

    ingested_at: datetime = Field(default_factory=_now)

    @field_validator("claim", "mechanism", "business_type", "product_price_band_ngn")
    @classmethod
    def _no_blank_prose(cls, v: str, info) -> str:
        """"This just works" is noise — a record with an empty mechanism is a note.
        Whitespace-only passes a plain `str` type but fails the workbook's rule."""
        if not v or not str(v).strip():
            raise ValueError(f"{info.field_name} is mandatory and cannot be blank")
        return str(v).strip()

    @model_validator(mode="after")
    def _budget_floor_required_unless_rejected(self) -> "Strategy":
        """Budget floors are in naira per day, always — "low budget" is not a
        precondition, ₦3,000/day is. The one principled exception is a record whose
        verdict is DOES_NOT_TRANSFER: a rejected claim has no budget at which it
        works, and the text columns express that as "Not applicable" while a numeric
        column has no such value. Those records are still kept and retrievable.

        Zero is a real floor, not a missing one: an organic tactic (WhatsApp Status
        sequencing, say) costs ₦0/day to run, and Jane needs those retrievable for a
        user with no paid budget at all."""
        if self.transfer_verdict is TransferVerdict.DOES_NOT_TRANSFER:
            return self
        if self.budget_floor_ngn_per_day is None:
            raise ValueError(
                "budget_floor_ngn_per_day is mandatory unless transfer_verdict is "
                f"'{TransferVerdict.DOES_NOT_TRANSFER.value}'"
            )
        if self.budget_floor_ngn_per_day < 0:
            raise ValueError("budget_floor_ngn_per_day cannot be negative")
        return self

    @property
    def is_ingestible(self) -> bool:
        """EXAMPLE rows set the standard for human seeders but are not corpus content —
        the workbook says to delete them before export, so ingestion drops them too
        rather than trusting that someone remembered."""
        return self.status is not StrategyStatus.EXAMPLE
