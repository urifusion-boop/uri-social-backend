# app/agents/content_calendar_v2/models.py
"""
Content Calendar V2 — Pydantic models.

New models this pass genuinely needs (confirmed via codebase search: no
ad-angle/ad-objective enum and no richer AdCopy shape exists anywhere else —
jane_ads/models.py's AdCopy has no angle, no cta, no short_copy). Everything
else (request bodies) mirrors the v1 shapes in complete_social_manager.py's
CalendarGenerateRequest/CalendarCreateDraftRequest.
"""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

AdAngle = Literal[
    "problem_first", "outcome_first", "social_proof",
    "offer", "urgency", "comparison", "objection_handling",
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


class PlanGenerateRequestV2(BaseModel):
    platforms: List[str] = ["facebook", "instagram"]
    force_regenerate: bool = False


class CreateDraftRequestV2(BaseModel):
    platforms: List[str] = ["facebook", "instagram"]
    include_images: bool = False


class RegenerateItemRequestV2(BaseModel):
    reason: str = ""   # optional — surfaced in the version_history entry
