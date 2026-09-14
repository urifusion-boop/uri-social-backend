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



class ConversationOutcome(str, Enum):
    """Did this conversation become a customer? The blocking dependency for the whole
    learning loop (spec §14.4): without it, both Jane and the platform optimise on
    cost per lead, which systematically selects for cheap enquiries that never buy."""
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


class OutcomeSetBy(str, Enum):
    OPERATOR = "operator"
    USER = "user"
    JANE_INFERRED = "jane_inferred"   # never counted as confirmed for re-weighting


class Conversation(BaseModel):
    conversation_id: str
    business_id: str
    ad_id: str
    campaign_id: str
    platform: Platform
    charged_ngn: float
    actual_platform_cost_ngn: Optional[float] = None
    at: datetime = Field(default_factory=_now)

    # Outcome capture (spec §14) — a field on the object that already exists, not a
    # new surface. The attribution chain campaign -> conversation was already here.
    outcome: Optional[ConversationOutcome] = None
    outcome_value_ngn: Optional[float] = None
    outcome_set_by: Optional[OutcomeSetBy] = None
    outcome_set_at: Optional[datetime] = None

    @property
    def outcome_is_confirmed(self) -> bool:
        """Jane may pre-fill from context (a customer asking for account details,
        confirming an order), but an inferred outcome is never treated as confirmed
        for corpus re-weighting (§14.3) — it is a labour saver, not evidence."""
        return (
            self.outcome is not None
            and self.outcome_set_by is not OutcomeSetBy.JANE_INFERRED
        )


class AuthorizationRecord(BaseModel):
    """Audit trail of the client's explicit grant (PRD Part D): run ads on their behalf,
    spend from their wallet, represent their business. Who / what / when."""
    business_id: str
    authorized_run_ads: bool = False
    authorized_spend_wallet: bool = False
    authorized_represent: bool = False
    granted_by: str = ""                # user id/email
    at: datetime = Field(default_factory=_now)


# ── Ad strategy corpus (ASC-SPEC-01 v2 / ASC-ENG-01 v1) ──────────────────────
# The curated corpus Jane retrieves from instead of planning on model priors.
#
# Enum VALUES are the spec's canonical snake_case, not the workbook's display
# strings. The sheet is a human surface; storage is the contract. `corpus.py`
# maps one to the other on import, so a label change in the workbook never
# silently rewrites stored data.

class StrategyCategory(str, Enum):
    OFFER_POSITIONING = "offer_positioning"
    AUDIENCE_CONSTRUCTION = "audience_construction"
    CREATIVE_FORMATS = "creative_formats"
    COPY_ANGLES = "copy_angles"
    BUDGET_PACING = "budget_pacing"
    MICRO_BUDGET_TESTING = "micro_budget_testing"
    CONVERSION_MECHANICS = "conversion_mechanics"
    RETARGETING = "retargeting_sequencing"
    PLATFORM_MECHANICS = "platform_mechanics"
    DIAGNOSTICS = "diagnostics"


class StrategyStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class EvidenceGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"

    @property
    def weight(self) -> float:
        """Spec §8.1. D is excluded upstream and must never be scored."""
        return {"A": 1.0, "B": 0.8, "C": 0.5, "D": 0.0}[self.value]


class MarketOrigin(str, Enum):
    """`nigeria` means evidence observed in a Nigerian account. `nigeria_desk_research`
    means reasoning about Nigeria from published sources. Spec §3.3 forbids collapsing
    them — the distinction is the only honest measure of local knowledge held, and only
    `nigeria` earns the origin bonus."""
    NIGERIA = "nigeria"
    NIGERIA_DESK_RESEARCH = "nigeria_desk_research"
    AFRICA_OTHER = "africa_other"
    US = "us"
    UK_EU = "uk_eu"
    ASIA = "asia"
    LATIN_AMERICA = "latin_america"
    GLOBAL_UNSPECIFIED = "global_unspecified"


class TransferVerdict(str, Enum):
    APPLIES_AS_IS = "applies_as_is"
    APPLIES_WITH_MODIFICATION = "applies_with_modification"
    DOES_NOT_TRANSFER = "does_not_transfer"


class LocalTestStatus(str, Enum):
    NOT_YET_TESTED = "not_yet_tested"
    TESTING = "testing"
    CONFIRMED_LOCALLY = "confirmed_locally"
    UNDERPERFORMED_LOCALLY = "underperformed_locally"
    RETIRED = "retired"


class ConversionLocation(str, Enum):
    WEBSITE = "website"
    APP = "app"
    MESSAGING = "messaging"
    LEAD_FORM = "lead_form"
    CALLS = "calls"
    ANY = "any"


class PooledAccountSafety(str, Enum):
    """Defaults to UNKNOWN, which blocks retrieval — fail closed (spec §3.2).
    At cold start most records carry it and nothing retrieves. That is correct
    behaviour, not a broken system (ENG §10)."""
    YES = "yes"
    NO = "no"
    REQUIRES_ISOLATION = "requires_isolation"
    UNKNOWN = "unknown"


class ConsumedBy(str, Enum):
    """Flow stage, not topic. `category` is a topical taxonomy; retrieval scopes by stage."""
    PLAN_GENERATION = "plan_generation"
    CREATIVE_BRIEF = "creative_brief"
    CAMPAIGN_STRUCTURE = "campaign_structure"
    VCE = "vce"
    DIAGNOSTICS = "diagnostics"
    REPORTING = "reporting"


class ExecutableVia(str, Enum):
    """Users never open Ads Manager. A ui_only tactic is team knowledge, not an
    executable strategy — retrievable at the diagnostics stage only (spec §7.1.10)."""
    API = "api"
    UI_ONLY = "ui_only"
    MANUAL = "manual"


class Requirement(str, Enum):
    OUTCOME_CAPTURE = "outcome_capture"
    WEBSITE_OR_PIXEL = "website_or_pixel"
    CUSTOMER_LIST = "customer_list"
    PARALLEL_ADSET_BUDGET = "parallel_adset_budget"
    CREATIVE_PRODUCTION = "creative_production"
    VIDEO_ASSET = "video_asset"
    EXISTING_WINNING_CREATIVE = "existing_winning_creative"
    # VSG-01 v3 §1.2/§6 — a format whose asset_source is upload_as_is or
    # recomposite must never surface for a business with no real photo to
    # build it from; substituting a generated stand-in for a physical
    # product the customer will actually receive is a misrepresentation,
    # not a style choice. Gate at retrieval, not at render (§6, §9).
    PRODUCT_PHOTO = "product_photo"
    # Same rule for formats that assert a specific customer's testimony
    # (Testimonial + Offer's person path, Text on a Face) — a generated
    # face paired with a first-person quote implies a customer who does
    # not exist (§1.2).
    REAL_CUSTOMER_PHOTO = "real_customer_photo"


class SalesCycle(str, Enum):
    SAME_DAY = "same_day"
    ONE_TO_SEVEN_DAYS = "1_7_days"
    ONE_TO_FOUR_WEEKS = "1_4_weeks"
    OVER_A_MONTH = "over_a_month"
    NOT_APPLICABLE = "not_applicable"


class StrategyPlatform(str, Enum):
    META = "meta"
    TIKTOK = "tiktok"
    GOOGLE = "google"
    LINKEDIN = "linkedin"
    SNAPCHAT = "snapchat"
    WHATSAPP = "whatsapp"
    CROSS_PLATFORM = "cross_platform"


class LocalEvidence(BaseModel):
    """Local-evidence state, ORTHOGONAL to approval status. ENG §1 names conflating
    the two as the most likely modelling error here, so they are separate objects.

    CONFIRMED_LOCALLY promotes the effective evidence grade to A (spec §8.1) — the
    inversion the whole corpus exists for. That is why promotion needs human
    confirmation and threshold floors: a bad promotion launders a guru assertion
    into local evidence.
    """
    test_status: LocalTestStatus = LocalTestStatus.NOT_YET_TESTED
    deployments: int = 0
    outcomes_recorded: int = 0
    positive_outcomes: int = 0
    result_notes: Optional[str] = None
    last_reviewed: Optional[datetime] = None

    @property
    def outcome_rate(self) -> Optional[float]:
        """Of deployments WITH a recorded outcome. Deployments lacking outcomes do not
        count toward promotion — a record can have 20 deployments and stay unpromotable
        (ENG §2 MIN_OUTCOMES_RECORDED)."""
        if not self.outcomes_recorded:
            return None
        return self.positive_outcomes / self.outcomes_recorded


class Strategy(BaseModel):
    """One tactic, at one version. Never a bundle — a source with six ideas is six
    records, because a bundle is unusable at retrieval.

    Immutable once approved (spec §3.3): an edit creates version + 1 at status=draft,
    and prior versions are retained so a plan generated in October is still
    explainable in January at the version it actually used.
    """

    # ── identity
    strategy_id: str
    version: int = 1

    # ── core
    status: StrategyStatus = StrategyStatus.DRAFT
    category: StrategyCategory
    claim: str
    mechanism: str
    evidence_grade: EvidenceGrade
    market_origin: MarketOrigin
    transfer_verdict: TransferVerdict
    modification_required: Optional[str] = None

    # ── preconditions
    business_types: list[str] = Field(default_factory=list)
    budget_floor_ngn_daily: Optional[float] = None
    platforms: list[StrategyPlatform] = Field(default_factory=list)
    funnel_stages: list[str] = Field(default_factory=list)
    price_band_min_ngn: Optional[int] = None
    price_band_max_ngn: Optional[int] = None
    sales_cycle: SalesCycle = SalesCycle.NOT_APPLICABLE

    # ── v2 additions (spec §3.2). Defaults fail closed, never open.
    conversion_location: list[ConversionLocation] = Field(default_factory=list)
    pooled_account_safe: PooledAccountSafety = PooledAccountSafety.UNKNOWN
    consumed_by: list[ConsumedBy] = Field(default_factory=list)
    implies_product_change: bool = False
    executable_via: ExecutableVia = ExecutableVia.API
    requires_sustained_days: int = 1
    requires: list[Requirement] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)

    # ── provenance
    source_type: Optional[str] = None
    source_reference: Optional[str] = None
    source_published_at: Optional[datetime] = None
    ingested_at: datetime = Field(default_factory=_now)
    ingested_by: str = "import"

    # ── local evidence
    local: LocalEvidence = Field(default_factory=LocalEvidence)

    staleness_review_due: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)

    @field_validator("claim", "mechanism")
    @classmethod
    def _no_blank_prose(cls, v: str, info) -> str:
        """"This just works" is noise — a record without a mechanism is a note."""
        if not v or not str(v).strip():
            raise ValueError(f"{info.field_name} is mandatory and cannot be blank")
        return str(v).strip()

    @model_validator(mode="after")
    def _modification_required_when_verdict_says_so(self) -> "Strategy":
        """Spec §8.2: returning the claim without its modification is a correctness
        bug — the unmodified version is frequently wrong here and occasionally
        harmful. Enforced at the record, so it cannot be lost downstream."""
        if (
            self.transfer_verdict is TransferVerdict.APPLIES_WITH_MODIFICATION
            and not (self.modification_required or "").strip()
        ):
            raise ValueError(
                "modification_required is mandatory when transfer_verdict is "
                "applies_with_modification"
            )
        return self

    @model_validator(mode="after")
    def _budget_floor_required_unless_rejected(self) -> "Strategy":
        """A rejected claim has no budget at which it works, and the sheet's text
        columns say "Not applicable" where a numeric column cannot (SEED-003).
        Zero is a real floor, not a missing one: an organic tactic costs ₦0/day
        (SEED-044), and Jane needs those for a user with no paid budget at all."""
        if self.transfer_verdict is TransferVerdict.DOES_NOT_TRANSFER:
            return self
        if self.budget_floor_ngn_daily is None:
            raise ValueError(
                "budget_floor_ngn_daily is mandatory unless transfer_verdict is "
                "does_not_transfer"
            )
        if self.budget_floor_ngn_daily < 0:
            raise ValueError("budget_floor_ngn_daily cannot be negative")
        return self

    @property
    def effective_grade(self) -> EvidenceGrade:
        """Spec §8.1 — local confirmation REPLACES the grade rather than modifying it.
        v1's additive modifier produced a tie, not the claimed inversion."""
        if self.local.test_status is LocalTestStatus.CONFIRMED_LOCALLY:
            return EvidenceGrade.A
        return self.evidence_grade
