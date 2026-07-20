"""
Jane + Ads — the interface contract (the seam between Shore's brain and Ibukun's
platform adapters).

Decision model (corrected): GOAL leads, PURCHASE BEHAVIOUR drives platform choice,
business type is only a hint that sets a default behaviour, and every decision is
made PER CAMPAIGN — the same business can land on different platforms across
campaigns. Nothing here depends on a live platform, so the whole Shore side is
buildable and testable today.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class Platform(str, Enum):
    META = "meta"
    GOOGLE = "google"
    TIKTOK = "tiktok"


class Goal(str, Enum):
    """The goal of THIS campaign — leads the whole decision."""
    MESSAGES = "messages"
    LEADS = "leads"
    BOOKINGS = "bookings"
    WALK_INS = "walk_ins"
    AWARENESS = "awareness"
    SALES = "sales"
    FOLLOWERS = "followers"


class PurchaseBehaviour(str, Enum):
    """How customers buy THIS thing — the real driver of platform choice."""
    SEARCH = "search"       # they actively search → Google
    DISCOVER = "discover"   # they find it while scrolling → Meta / TikTok
    MIXED = "mixed"         # both → Meta + Google if budget allows


class CreativeKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    NONE = "none"           # Google Search needs no creative


class ABTestScope(str, Enum):
    NONE = "none"
    AUDIENCE = "audience"
    AUDIENCE_AND_CREATIVE = "audience_and_creative"


class CampaignObjective(str, Enum):
    CONVERSATIONS = "conversations"     # Click-to-WhatsApp — the only objective we run


class PlanDecision(str, Enum):
    PLAN = "plan"
    ADVISE = "advise"


class GeoMode(str, Enum):
    OWN_RADIUS = "own_radius"       # business PULLS customers to its spot (salon, clinic)
    WATERING_HOLE = "watering_hole" # business GOES to where customers gather (realtor, B2B)


class PinSource(str, Enum):
    GEOCODED = "geocoded"           # AI-proposed, then validated to real coordinates
    FALLBACK = "fallback"           # broad known-good area (nothing validated)
    PERFORMANCE = "performance"     # a pin that actually converted before (the moat)


class GeoPin(BaseModel):
    """A validated, targetable location. Coordinates come from geocoding — never the
    LLM — so Jane can't pin an imaginary street."""
    name: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius_km: float = 2.0
    source: PinSource = PinSource.GEOCODED
    reason: str = ""                # why this pocket (e.g. "commercial street, offices")


class GeoPlan(BaseModel):
    mode: GeoMode
    city: str = ""
    pins: list[GeoPin] = Field(default_factory=list)
    fallback_area: str = ""         # set when nothing could be validated → broad area
    explanation: str = ""


# ── Inputs ──────────────────────────────────────────────────────────────────

class CreativeContext(BaseModel):
    kind: CreativeKind = CreativeKind.IMAGE
    has_video: bool = False


class CampaignRequest(BaseModel):
    """One campaign's plain-language ask, normalized. Decisions attach HERE, never to
    the business — the same business_id can send many requests with different goals."""
    business_id: str
    business_name: str = ""
    category: str = ""                      # hint only → sets a default behaviour
    description: str = ""

    goal: Goal = Goal.MESSAGES              # leads everything
    budget_ngn: float = Field(..., gt=0)
    creative: CreativeContext = Field(default_factory=CreativeContext)

    # Behaviour overrides (PRD §3): the user's stated behaviour and goal-implications
    # override the business-type default.
    stated_behaviour: Optional[PurchaseBehaviour] = None
    is_new_thing: bool = False              # a service nobody searches for yet → discover
    has_existing_demand: bool = False       # people already look for this → search

    geo: str = ""                           # e.g. "5km around Surulere" (targeting, not platform)
    whatsapp_number: str = ""


# ── Outputs ───────────────────────────────────────────────────────────────────

class PlatformPlan(BaseModel):
    platform: Platform
    budget_ngn: float
    days: int
    variants: int
    test_scope: ABTestScope
    objective: CampaignObjective = CampaignObjective.CONVERSATIONS


class CampaignPlan(BaseModel):
    business_id: str
    goal: Goal
    behaviour: PurchaseBehaviour            # the resolved behaviour that drove the choice
    platforms: list[PlatformPlan]
    per_business_cap_ngn: float
    account_cap_ngn: float
    geo: Optional[GeoPlan] = None           # attached after platform selection
    objective: CampaignObjective = CampaignObjective.CONVERSATIONS
    explanation: str = ""                   # required plain-language "why" (PRD §6)
    trace: list[str] = Field(default_factory=list)
    page_id: str = ""                       # connected Facebook Page — the real Meta adapter
                                             # needs this for Click-to-WhatsApp's promoted_object;
                                             # attached after platform selection, same as geo
    creative: Optional["AdCreative"] = None # the actual ad (image/video + copy) from creative.py —
                                             # Meta rejects link-ad creation with no real media
                                             # attached, so the real adapter needs this, not just
                                             # the platform/budget decision


class SpendAuthorization(BaseModel):
    business_id: str
    funded_amount_ngn: float
    account_cap_ngn: float


class PlanAdvice(BaseModel):
    reason: str
    suggested_min_ngn: float
    can_pool: bool = False
    trace: list[str] = Field(default_factory=list)


class PlanResult(BaseModel):
    decision: PlanDecision
    plan: Optional[CampaignPlan] = None
    advice: Optional[PlanAdvice] = None


# ── Ad creative (split-doc 1.6) ───────────────────────────────────────────────

class AdCopy(BaseModel):
    """The written parts of an ad, plus the prompt used to generate its image."""
    headline: str = ""              # ≤ ~5 words
    primary_text: str = ""          # 1–2 sentence body
    image_prompt: str = ""          # what the creative image should show


class CreativeSource(str, Enum):
    """Mirrors PRD Part D2 — the three ways a creative's image is sourced."""
    GENERATE = "generate"    # Jane generates it via the brand playbook engine (default)
    UPLOAD = "upload"        # the user's own uploaded photo/video
    DRAFT = "draft"          # an existing content draft the user already liked


class AdCreative(BaseModel):
    """A complete ad ready to submit: a creative (image or video) + copy + the
    WhatsApp CTA. Every ad routes to WhatsApp — the CTA is fixed, never up to the
    user. GENERATE always produces an image (gpt-image-1 has no video mode); UPLOAD
    and DRAFT can carry either — `is_video` says which."""
    image_url: str = ""             # final creative media URL, hosted on Cloudinary
    is_video: bool = False           # True when image_url is actually a video
    headline: str = ""
    primary_text: str = ""
    cta: str = "Send WhatsApp Message"
    source: CreativeSource = CreativeSource.GENERATE
    generated: bool = True          # False when there's no media → copy-only fallback


# ── Events (adapter → Shore) ──────────────────────────────────────────────────

class ConversationDelivered(BaseModel):
    business_id: str
    ad_id: str
    campaign_id: str
    platform: Platform
    at: datetime
    charge_ngn: float


class PerAdSpend(BaseModel):
    business_id: str
    ad_id: str
    campaign_id: str
    platform: Platform
    spend_ngn: float
    at: datetime


class LaunchResult(BaseModel):
    campaign_id: str
    ad_ids: dict[str, str]
    platforms: list[Platform]
    launched: bool = True
