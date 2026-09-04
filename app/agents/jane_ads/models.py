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
    TRAFFIC = "traffic"


class OfferType(str, Enum):
    """WHAT's being promoted — distinct from Goal (the campaign's intent). Asked
    before budget so Jane understands the offer before talking money (PRD feedback:
    objective-first flow)."""
    PRODUCT = "product"
    SERVICE = "service"
    PROMOTION = "promotion"
    EVENT = "event"
    LAUNCH = "launch"
    AWARENESS = "awareness"
    OTHER = "other"


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
    CONVERSATIONS = "conversations"     # Click-to-WhatsApp — every goal except FOLLOWERS
    ENGAGEMENT = "engagement"           # Page-follower growth (Goal.FOLLOWERS) — no WhatsApp


class PlanDecision(str, Enum):
    PLAN = "plan"
    ADVISE = "advise"


class GeoMode(str, Enum):
    OWN_RADIUS = "own_radius"       # business PULLS customers to its spot (salon, clinic)
    WATERING_HOLE = "watering_hole" # business GOES to where customers gather (realtor, B2B)
    MIXED = "mixed"                 # tight radius AND focused on specific pockets within it
                                     # (e.g. a premium daycare: drop-off distance + affluent estates)
    NON_LOCAL = "non_local"         # geography barely relevant — online-only, nationwide
                                     # delivery, or a customer base outside the country


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
    offer_type: Optional[OfferType] = None  # WHAT's being promoted, distinct from goal
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
    budget_tier: str = ""                   # "starter" | "standard" | "growth" — same
                                             # ₦10k/₦20k boundaries the A/B test scope
                                             # already uses (constants.AB_LIGHT_TEST_NGN/
                                             # AB_FULL_TEST_NGN), so the label always
                                             # matches the variant/test-scope decision
    estimated_conversations: Optional[int] = None  # ALWAYS an estimate, never a promise
                                             # (PRD §3.3) — total budget ÷ this business's
                                             # real per-conversation price; attached
                                             # regardless of whether the user stated a
                                             # budget or a desired outcome
    geo: Optional[GeoPlan] = None           # attached after platform selection
    objective: CampaignObjective = CampaignObjective.CONVERSATIONS
    explanation: str = ""                   # required plain-language "why" (PRD §6)
    trace: list[str] = Field(default_factory=list)
    page_id: str = ""                       # connected Facebook Page — owns the ad's identity
                                             # (name/avatar shown on the ad); attached after
                                             # platform selection, same as geo
    whatsapp_number: str = ""               # the brand's own WhatsApp number (digits, country-
                                             # coded) leads route to — the ad links to
                                             # wa.me/<this>, so chats reach the brand directly
                                             # instead of the shared Page's inbox. Only
                                             # meaningful when destination_type == "whatsapp".
    destination_type: str = "whatsapp"      # where the tap goes: whatsapp | website |
                                             # instagram_dm (destination.DestinationType).
                                             # Defaults to whatsapp so every plan built
                                             # before this field existed behaves as before.
    destination_link: str = ""              # the ACTUAL resolved https link the adapters put
                                             # on the ad (wa.me/…, the site, ig.me/m/…, or
                                             # whatever URL the user pasted), frozen at
                                             # plan-build time like whatsapp_number
    destination_cta: str = ""               # which button the user picked, as a key into
                                             # destination.CTA_CHOICES ("shop_now", "book_now",
                                             # …). Ignored for a WhatsApp destination, which
                                             # uses Meta's own native button.
    creative: Optional["AdCreative"] = None # the actual ad (image/video + copy) from creative.py —
                                             # Meta rejects link-ad creation with no real media
                                             # attached, so the real adapter needs this, not just
                                             # the platform/budget decision
    shoot_script: Optional["ShootScript"] = None  # Path C — set only when creative.video_recommendation
                                             # fired; the user can film this and swap it in via
                                             # POST /meta/plan/{id}/creative before launching
    google_keyword_context: Optional[dict] = None  # {category, description, geo} — CampaignPlan
                                             # carries no raw business description otherwise (only
                                             # goal/behaviour), and GoogleAdsAdapter's keyword
                                             # translation needs it. Populated by the router at
                                             # plan-build time; None for every non-Google plan.
    audience_targeting: dict = Field(default_factory=dict)  # Meta-shaped demographic/
                                             # interest targeting (age_min/age_max/genders/
                                             # flexible_spec) resolved from the selected
                                             # variant's audience_segment (or the brand's own
                                             # target_audience) via audience_targeting.py.
                                             # {} — broad on this axis — when nothing resolved;
                                             # merged with geo.meta_targeting_from_geo() at
                                             # launch, never replaces it.


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


# ── Multi-Plan Audience Variants (plan-variants spec v1.0.0) ──────────────────
# A candidate STRATEGY — a different buyer, trigger, place, and usually message —
# produced after strategy extraction and BEFORE the platform/budget decision
# (decision_engine.plan_campaign runs once PER selected variant, not once overall).
# Deliberately NOT a CampaignPlan: nothing here has a platform/budget/geo-pin
# decision yet, only the audience hypothesis a client picks from.

class PlanVariant(BaseModel):
    rank: int                          # 1 = best; also the display order
    recommended: bool = False          # exactly one variant should carry this
    who_its_for: str                   # customer's OWN words — Zone B material, e.g.
                                        # "people fitting out a new place" — NEVER the
                                        # segment label (spec §4.3)
    audience_segment: str              # the underlying targeting label — Zone A, never
                                        # written into copy (e.g. "homeowners 30-55")
    geo_pockets: list[str] = Field(default_factory=list)  # named areas — Zone A,
                                        # "suggestions to confirm" not hard commitments
    trigger: str                       # the buying-moment hypothesis, not a description
                                        # of the audience — e.g. "solar gets bought at the
                                        # moment someone is setting up a new place"
    why_this_could_work: str           # plain-language reasoning tying trigger to money
    trade_off: str                     # MANDATORY (spec §4.4) — every plan has one
    needs_video: bool = False          # creative feasibility signal (spec §4.3 "creative")
    budget_alone_ngn: float = 0.0      # what this plan would cost run alone
    budget_shared_ngn: Optional[float] = None  # what it would cost split with one other


class StrategyCitation(BaseModel):
    """A corpus record that shaped a plan, pinned at the VERSION used. Citing the id
    alone makes a plan unexplainable within one edit cycle (ASC-ENG-01 §3)."""
    record_id: str
    version: int
    stage: str
    score: float


class PlanVariantSet(BaseModel):
    """The full ranked set Jane presents, plus the computed (never generated)
    selection rule for this budget (spec §6.1)."""
    variants: list[PlanVariant]
    # ASC-SPEC-01 v2 §8.4 — 'none' must be visible, never silent: if retrieval
    # returned nothing Jane fell back to model priors and the plan should say so.
    corpus_coverage: str = "none"          # none | partial | full
    corpus_citations: list[StrategyCitation] = Field(default_factory=list)
    recommendation_reason: str = ""    # argued, not asserted (spec §3) — why the
                                        # recommended variant beats the others
    max_selectable: int = 1
    selection_rule_reason: str = ""    # e.g. "at ₦12,000 I'd pick just one — split it
                                        # and neither gets enough to work properly"


# ── Ad creative (split-doc 1.6) ───────────────────────────────────────────────

class AdCopy(BaseModel):
    """The written parts of an ad, plus the prompt used to generate its image."""
    headline: str = ""              # ≤ ~5 words
    primary_text: str = ""          # 1–2 sentence body
    image_prompt: str = ""          # what the creative image should show
    video_recommended: bool = False       # creative-type reasoning (PRD §4.1) — true
                                           # when a video would clearly serve this
                                           # campaign better than the photo we're
                                           # about to generate (gpt-image-1 has no
                                           # video mode; this is a heads-up, not a
                                           # capability)
    video_recommendation_reason: str = ""


class CreativeSource(str, Enum):
    """Mirrors PRD Part D2, extended by the creative brief spec's four asset paths,
    plus a fifth added for the static ad format library (VSG-01 v3 §1.2)."""
    GENERATE = "generate"        # Jane generates it via the brand playbook engine (default)
    UPLOAD = "upload"            # the user's own uploaded photo/video, used as-is
    DRAFT = "draft"              # an existing content draft the user already liked
    RECOMPOSITE = "recomposite"  # the user's own real product photo, background cleaned/
                                 # replaced — the product itself is never regenerated
                                 # (product truthfulness rule, creative brief spec §7.1)
    DRAWN = "drawn"              # entirely composited in Layer 4, no photography at all —
                                 # receipts, comparison tables, chat-thread mockups. Never
                                 # a stand-in for a real product photo; only for formats
                                 # that were never going to show one (VSG-01 v3 §1.2, §2.5,
                                 # §2.4, §2.6).


class AdCreative(BaseModel):
    """A complete ad ready to submit: a creative (image or video) + copy + the
    WhatsApp CTA. Every ad routes to WhatsApp — the CTA is fixed, never up to the
    user. GENERATE always produces an image (gpt-image-1 has no video mode); UPLOAD,
    DRAFT and RECOMPOSITE can carry either — `is_video` says which."""
    image_url: str = ""             # final creative media URL, hosted on Cloudinary
    is_video: bool = False           # True when image_url is actually a video
    # Which corpus records shaped this copy, pinned at version. Without this there is
    # no way to tell from the outside whether the corpus reached the creative stage or
    # was silently ignored — the position the first live test left us in.
    corpus_coverage: str = "none"                     # none | partial | full
    corpus_citations: list[StrategyCitation] = Field(default_factory=list)
    headline: str = ""
    primary_text: str = ""
    cta: str = "Send WhatsApp Message"
    source: CreativeSource = CreativeSource.GENERATE
    video_recommendation: str = ""  # set only on a GENERATE creative when a video
                                     # would clearly serve better — a heads-up so the
                                     # user can choose to upload one instead; never
                                     # set for UPLOAD/DRAFT (the media choice is
                                     # already made there)
    generated: bool = True          # False when there's no media → copy-only fallback

    # Attribute tagging (creative brief spec §11/§6) — cheap, computed at assembly
    # time (never model-generated), so later analysis can ask "does stating the
    # price help", "which asset path performs", etc. without re-deriving from raw text.
    asset_path: CreativeSource = CreativeSource.GENERATE  # same value as `source` today;
                                                            # kept as its own field since
                                                            # `source` may grow meanings
                                                            # (e.g. a future re-edit) that
                                                            # shouldn't retroactively change
                                                            # what asset_path recorded
    shows_price: bool = False       # a ₦ amount appears in the copy
    shows_service_area: bool = False  # the resolved service_area string appears in the copy
    copy_length: int = 0             # len(headline) + len(primary_text), for quick analysis


# ── Path C: script-and-shoot (PRD §4.1) ───────────────────────────────────────

class ShootShot(BaseModel):
    """One shot in a phone-filmable script — no crew, no equipment assumed."""
    direction: str = ""      # what to film / how to frame it (e.g. "you, mid-shot, at the counter")
    say: str = ""            # what to say on camera, if anything; empty for a silent b-roll shot
    seconds: int = 5         # rough duration, so the whole script sums to a realistic ad length


class ShootScript(BaseModel):
    """Jane's shoot script — written when a video was recommended over a photo, so
    the business owner can film it themselves instead of relying on the (photo-only)
    AI generator. Filmed footage is then uploaded via the existing upload path and
    folded back into the pending plan; this is guidance for the shoot, not media."""
    hook_line: str = ""                          # the first ~2 seconds — what stops the scroll
    shots: list[ShootShot] = Field(default_factory=list)
    caption_idea: str = ""                       # optional on-screen caption/text overlay idea


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
