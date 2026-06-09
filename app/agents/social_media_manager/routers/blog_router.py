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

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.domain.responses.uri_response import UriResponse

from ..services.writing_dna_service import WritingDNAService
from ..services.blog_generation_service import BlogGenerationService

router = APIRouter(prefix="/blog", tags=["Blog Generator"])


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
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Submit the 16-question Writing DNA quiz.
    Optionally include a writing sample for even more accurate voice matching.
    Returns the generated Writing DNA prompt (200-400 words of voice directives).
    """
    user_id = _user_id(token)
    answers = body.quiz_answers.dict()
    return await WritingDNAService.save(
        user_id=user_id,
        quiz_answers=answers,
        writing_sample=body.writing_sample,
        db=db,
    )


@router.get("/writing-dna")
async def get_writing_dna(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get the current Writing DNA profile for this user."""
    user_id = _user_id(token)
    return await WritingDNAService.get(user_id=user_id, db=db)


@router.post("/generate")
async def generate_blog(
    body: BlogGenerateRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Generate a blog post using the user's Writing DNA voice profile.
    If no DNA has been set up yet, falls back to a sensible default voice.
    Returns title, meta description, and full blog content in Markdown.
    """
    user_id = _user_id(token)
    return await BlogGenerationService.generate(
        user_id=user_id,
        topic=body.topic,
        primary_keyword=body.primary_keyword,
        secondary_keywords=body.secondary_keywords,
        word_count=body.word_count,
        db=db,
    )


@router.get("/posts")
async def list_blog_posts(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """List all generated blog posts for this user (content excluded for performance)."""
    user_id = _user_id(token)
    return await BlogGenerationService.list_posts(user_id=user_id, db=db)


@router.get("/posts/{blog_id}")
async def get_blog_post(
    blog_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get a single blog post with full content."""
    user_id = _user_id(token)
    return await BlogGenerationService.get_post(blog_id=blog_id, user_id=user_id, db=db)


@router.patch("/posts/{blog_id}")
async def update_blog_post(
    blog_id: str,
    body: BlogUpdateRequest,
    background_tasks: BackgroundTasks,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Save user edits to a blog post.
    Diffs the original against the edited version in the background and
    updates the Writing DNA to improve future generations.
    """
    user_id = _user_id(token)

    # Fetch original before saving the edit
    original_doc = await db["blog_posts"].find_one({"id": blog_id, "user_id": user_id})
    original_content = original_doc.get("current_content", "") if original_doc else ""

    result = await BlogGenerationService.update_post(
        blog_id=blog_id,
        user_id=user_id,
        new_content=body.content,
        new_title=body.title,
        db=db,
    )

    # Kick off voice learning in the background (never blocks the response)
    if original_content and original_content.strip() != body.content.strip():
        background_tasks.add_task(
            WritingDNAService.learn_from_edits,
            user_id=user_id,
            original_content=original_content,
            edited_content=body.content,
            db=db,
        )

    return result


@router.post("/posts/{blog_id}/feedback")
async def record_blog_feedback(
    blog_id: str,
    body: BlogFeedbackRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Record 'Does this sound like you?' feedback.
    Thumbs-down with issues helps refine the Writing DNA over time.
    """
    user_id = _user_id(token)
    return await BlogGenerationService.record_feedback(
        blog_id=blog_id,
        user_id=user_id,
        rating=body.rating,
        issues=body.issues,
        db=db,
    )


@router.post("/posts/{blog_id}/publish")
async def publish_blog_post(
    blog_id: str,
    body: BlogPublishRequest,
    background_tasks: BackgroundTasks,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """
    Mark a blog post as published.
    Triggers §8.3 sample accumulation in the background — the published content
    is analysed and appended to the Writing DNA so future posts improve.
    """
    user_id = _user_id(token)

    # Capture final content before marking published (may include user edits)
    blog_doc = await db["blog_posts"].find_one({"id": blog_id, "user_id": user_id})
    published_content = (blog_doc or {}).get("current_content", "")

    result = await BlogGenerationService.publish_post(
        blog_id=blog_id,
        user_id=user_id,
        published_url=body.published_url,
        db=db,
    )

    # §8.3 — accumulate this post as a writing sample in the background
    if published_content and result.get("status"):
        background_tasks.add_task(
            WritingDNAService.accumulate_published_sample,
            user_id=user_id,
            published_content=published_content,
            db=db,
        )

    return result
