"""
Blog Router
URI Social — Writing DNA Blog Generator

Endpoints:
  POST   /blog/writing-dna/quiz        — Submit quiz, generate & save Writing DNA
  GET    /blog/writing-dna             — Get current Writing DNA
  POST   /blog/generate                — Generate a blog post
  GET    /blog/posts                   — List all blog posts
  GET    /blog/posts/{blog_id}         — Get a single post
  PATCH  /blog/posts/{blog_id}         — Update (user edits) + trigger voice learning
  POST   /blog/posts/{blog_id}/feedback — Record thumbs-up / thumbs-down
  POST   /blog/posts/{blog_id}/publish  — Mark as published
"""

from fastapi import APIRouter, Depends, BackgroundTasks
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from app.dependencies import get_db_dependency, get_active_brand_context
from app.core.auth_bearer import JWTBearer
from app.domain.responses.uri_response import UriResponse

from ..services.writing_dna_service import WritingDNAService
from ..services.blog_generation_service import BlogGenerationService
from app.services.AgencyCreditService import AgencyCreditService

router = APIRouter(prefix="/blog", tags=["Blog Generator"])

BLOG_CREDIT_COST = 1.0


def _user_id(token: dict) -> str:
    claims = token.get("claims", {})
    uid = claims.get("userId")
    if not uid:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="User ID not found in token")
    return uid


# ── Request / Response models ──────────────────────────────────────────────

class QuizAnswers(BaseModel):
    q1: str = Field(..., pattern="^[ABCD]$", description="Opening energy")
    q2: str = Field(..., pattern="^[ABCD]$", description="Sentence structure")
    q3: str = Field(..., pattern="^[ABCD]$", description="Teaching style")
    q4: str = Field(..., pattern="^[ABCD]$", description="Jargon level")
    q5: str = Field(..., pattern="^[ABCD]$", description="Headline preference")
    q6: str = Field(..., pattern="^[ABCD]$", description="Humour level")
    q7: str = Field(..., pattern="^[ABCD]$", description="Confrontation style")
    q8: str = Field(..., pattern="^[ABCD]$", description="Vulnerability level")
    q9: str = Field(..., pattern="^[ABCD]$", description="Pacing / paragraph length")
    q10: str = Field(..., pattern="^[ABCD]$", description="Closing style")
    q11: str = Field(..., pattern="^[ABCD]$", description="Pidgin level")
    q12: str = Field(..., pattern="^[ABCD]$", description="Reference universe")
    q13: str = Field(..., pattern="^[ABCD]$", description="Edge level")
    q14: str = Field(..., pattern="^[ABCD]$", description="Reader relationship")
    q15: str = Field(..., pattern="^[ABCD]$", description="Core archetype")
    q16: str = Field(default="", description="Aspirational writers (free text, comma-separated)")


class WritingDNARequest(BaseModel):
    quiz_answers: QuizAnswers
    writing_sample: Optional[str] = Field(
        default=None,
        max_length=3000,
        description="Optional writing sample — paste a paragraph you wrote that sounds like you",
    )


class BlogGenerateRequest(BaseModel):
    topic: str = Field(..., min_length=5, max_length=200)
    primary_keyword: str = Field(..., min_length=2, max_length=100)
    secondary_keywords: List[str] = Field(default_factory=list, max_length=10)
    word_count: int = Field(default=800, ge=300, le=3000)


class BlogUpdateRequest(BaseModel):
    content: str = Field(..., min_length=50)
    title: Optional[str] = Field(default=None, max_length=200)


class BlogFeedbackRequest(BaseModel):
    rating: str = Field(..., pattern="^(up|down)$")
    issues: Optional[List[str]] = Field(
        default=None,
        description="If rating=down: 'too_formal', 'too_casual', 'too_generic', 'not_my_style'",
    )


class BlogPublishRequest(BaseModel):
    published_url: Optional[str] = Field(default=None, max_length=500)


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post("/writing-dna/quiz")
async def submit_writing_dna_quiz(
    body: WritingDNARequest,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Submit the 16-question Writing DNA quiz.
    Optionally include a writing sample for even more accurate voice matching.
    Returns the generated Writing DNA prompt (200-400 words of voice directives).
    """
    return await WritingDNAService.save(
        user_id=ctx["user_id"],
        quiz_answers=body.quiz_answers.dict(),
        writing_sample=body.writing_sample,
        db=db,
        brand_id=ctx["brand_id"],
    )


@router.get("/writing-dna")
async def get_writing_dna(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get the current Writing DNA profile for the active brand."""
    return await WritingDNAService.get(user_id=ctx["user_id"], db=db, brand_id=ctx["brand_id"])


@router.post("/generate")
async def generate_blog(
    body: BlogGenerateRequest,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Generate a blog post using the active brand's Writing DNA voice profile.
    Credits are billed to the agency wallet (agency brand) or the user wallet (solo).
    Returns title, meta description, and full blog content in Markdown.
    """
    brand_id = ctx["brand_id"]
    user_id = ctx["user_id"]

    # Gate on credit availability before spending compute (PRD §4.5 — never fail silently)
    avail = await AgencyCreditService.check_availability(brand_id, BLOG_CREDIT_COST, db)
    if not avail["allowed"]:
        return UriResponse.error_response(
            f"Cannot generate: {avail['reason']}. "
            f"{'Top up the agency wallet' if avail['reason'] == 'agency_wallet_empty' else 'Raise the brand cap or wait for next month'}."
        )

    result = await BlogGenerationService.generate(
        user_id=user_id,
        topic=body.topic,
        primary_keyword=body.primary_keyword,
        secondary_keywords=body.secondary_keywords,
        word_count=body.word_count,
        db=db,
        brand_id=brand_id,
    )

    # Deduct only on success
    if result.get("status"):
        await AgencyCreditService.deduct_for_brand(
            brand_id=brand_id, credits=BLOG_CREDIT_COST, operation="blog_generation",
            user_id=user_id, db=db,
        )

    return result


@router.get("/posts")
async def list_blog_posts(
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """List all generated blog posts for the active brand (content excluded)."""
    return await BlogGenerationService.list_posts(user_id=ctx["user_id"], db=db, brand_id=ctx["brand_id"])


@router.get("/posts/{blog_id}")
async def get_blog_post(
    blog_id: str,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get a single blog post with full content (scoped to the active brand)."""
    return await BlogGenerationService.get_post(
        blog_id=blog_id, user_id=ctx["user_id"], db=db, brand_id=ctx["brand_id"]
    )


@router.patch("/posts/{blog_id}")
async def update_blog_post(
    blog_id: str,
    body: BlogUpdateRequest,
    background_tasks: BackgroundTasks,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Save user edits to a blog post.
    Diffs the original against the edited version in the background and
    updates the active brand's Writing DNA to improve future generations.
    """
    brand_id = ctx["brand_id"]
    user_id = ctx["user_id"]

    original_doc = await db["blog_posts"].find_one({"id": blog_id, "brand_id": brand_id})
    original_content = original_doc.get("current_content", "") if original_doc else ""

    result = await BlogGenerationService.update_post(
        blog_id=blog_id, user_id=user_id, new_content=body.content,
        new_title=body.title, db=db, brand_id=brand_id,
    )

    if original_content and original_content.strip() != body.content.strip():
        background_tasks.add_task(
            WritingDNAService.learn_from_edits,
            user_id=user_id, original_content=original_content,
            edited_content=body.content, db=db, brand_id=brand_id,
        )

    return result


@router.post("/posts/{blog_id}/feedback")
async def record_blog_feedback(
    blog_id: str,
    body: BlogFeedbackRequest,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Record 'Does this sound like you?' feedback (scoped to active brand)."""
    return await BlogGenerationService.record_feedback(
        blog_id=blog_id, user_id=ctx["user_id"], rating=body.rating,
        issues=body.issues, db=db, brand_id=ctx["brand_id"],
    )


@router.post("/posts/{blog_id}/publish")
async def publish_blog_post(
    blog_id: str,
    body: BlogPublishRequest,
    background_tasks: BackgroundTasks,
    ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Mark a blog post as published.
    Triggers §8.3 sample accumulation in the background — the published content
    is analysed and appended to the active brand's Writing DNA.
    """
    brand_id = ctx["brand_id"]
    user_id = ctx["user_id"]

    blog_doc = await db["blog_posts"].find_one({"id": blog_id, "brand_id": brand_id})
    published_content = (blog_doc or {}).get("current_content", "")

    result = await BlogGenerationService.publish_post(
        blog_id=blog_id, user_id=user_id, published_url=body.published_url,
        db=db, brand_id=brand_id,
    )

    if published_content and result.get("status"):
        background_tasks.add_task(
            WritingDNAService.accumulate_published_sample,
            user_id=user_id, published_content=published_content,
            db=db, brand_id=brand_id,
        )

    return result
