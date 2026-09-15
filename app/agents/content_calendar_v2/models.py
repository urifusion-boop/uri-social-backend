# app/agents/content_calendar_v2/models.py
"""
Content Calendar V2 — Pydantic models.

Rewritten alongside the Creative Intelligence Engine rewrite (see
creative_framework.py and content_calendar_v2_service.py's pipeline) —
against the "Living Content Calendar & Creative Intelligence Engine" PRD.
See /Users/macintoshhd/.claude/plans/enchanted-wiggling-treehouse.md for the
full plan this was built against.

AdAngle expanded from the old 7-value, content-type-derived enum (which only
ever reached 4/7 values in practice, via the now-deleted
_ANGLE_BY_CONTENT_TYPE map) to the PRD's own 10-value Ad Angle library
(§26), now derived from the item's own creative_angle (the 27-value
Layer-3 angle from creative_framework.py) via _derive_ad_angle(), not from
content_type.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

# PRD §26 — Ad Angle Library. Distinct from (and derived from) the much
# larger 27-value creative_angle on ContentItem itself — an item's creative
# angle is about the ORGANIC idea, this is about how to pitch it as a paid ad.
AdAngle = Literal[
    "problem", "outcome", "proof", "offer", "objection",
    "comparison", "urgency", "convenience", "transformation",
    "product_demonstration",
]


class AdCopyV2(BaseModel):
    headline: str = ""
    primary_text: str = ""
    short_copy: str = ""   # short version for square/story placements — PRD §20
    cta: str = ""
    image_prompt: str = ""


class AdOpportunityV2(BaseModel):
    is_ad_candidate: bool = False
    score: float = 0.0     # 0-100, rule-based — PRD §19
    angle: Optional[AdAngle] = None
    ad_copy: Optional[AdCopyV2] = None
    reason: str = ""        # why this scored the way it did — feeds "why this post?" (§36)


class SelectionScore(BaseModel):
    """PRD §11-12 candidate scoring — exactly these 9 dimensions, deliberately
    NEVER performance_score/google_trends_score/engagement_score/
    search_volume_score (PRD §2/§12 non-negotiable — enforced again at the
    code level in _score_candidates, not just by this model's shape)."""
    strategic_relevance: float = 0.0
    audience_relevance: float = 0.0
    creative_strength: float = 0.0
    distinctiveness: float = 0.0
    brand_fit: float = 0.0
    commercial_relevance: float = 0.0
    asset_feasibility: float = 0.0
    context_relevance: float = 0.0
    repetition_risk: float = 0.0
    diversity_gain: float = 0.0   # computed at selection time, not by the scoring LLM call


class CreativeDeviceV2(BaseModel):
    category: str = ""   # story | visual | conversational | psychological | structural
    device: str = ""      # key from creative_framework.CREATIVE_DEVICES[category]
    label: str = ""


class PlanGenerateRequestV2(BaseModel):
    platforms: List[str] = ["facebook", "instagram"]
    force_regenerate: bool = False


class CreateDraftRequestV2(BaseModel):
    platforms: List[str] = ["facebook", "instagram"]
    include_images: bool = False


class RegenerateItemRequestV2(BaseModel):
    reason: str = ""   # optional — surfaced in the version_history entry
