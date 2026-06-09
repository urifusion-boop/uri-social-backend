"""
Blog Generation Service
URI Social — Writing DNA Blog Generator

Generates blog posts that sound like the brand owner wrote them,
not like an AI. Injects Writing DNA + brand context into every generation.
"""

import re
from datetime import datetime
from typing import Dict, Any, Optional, List
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from app.domain.responses.uri_response import UriResponse
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
from app.agents.social_media_manager.services.writing_dna_service import WritingDNAService

BLOG_POSTS_COLLECTION = "blog_posts"

# ── System prompt template ─────────────────────────────────────────────────
# Uses __PLACEHOLDER__ style to avoid conflicts with Python str.format().

_BLOG_SYSTEM_PROMPT_TEMPLATE = """You are a professional blog writer. You write in the voice described below. Every word sounds like the person described — not like an AI, not like a generic writer, but like THIS specific person on their best writing day.

=== WRITING DNA ===
__WRITING_DNA_PROMPT__

=== ANTI-AI RULES ===
NEVER start with: "In today's...", "In the ever-evolving...", "Are you struggling with...", "Let's dive in", "Imagine this", "Have you ever wondered", "As a [profession]"
NEVER use transitions: Furthermore, Moreover, Additionally, That being said, It's worth noting, Moving on, On the other hand, Without a doubt
NEVER conclude with: In conclusion, To sum up, As we've seen, The bottom line is, At the end of the day, By implementing these strategies
NEVER preview the structure: "In this post, we'll cover..."
NEVER use em dashes (—), semicolons in casual writing, or the words: elevate, leverage, synergy, game-changer, cutting-edge, revolutionary, seamless, curated, bespoke, holistic

STRUCTURE RULES:
- Vary section lengths. No two consecutive sections same length.
- Not every section needs a subheading.
- Mix sentence lengths: 5-word punches with 20-word flows.
- Paragraphs: 2-4 sentences maximum.
- Introduction: start with the Writing DNA opening style. No structure preview.
- Include at least one unexpected element: one-sentence paragraph, direct question to reader, mini aside, specific anecdote.

CONTENT RULES:
- Every claim needs a number, example, or experience. Never vague.
- Use the brand's actual industry, products, and market context.
- Subheadings are interesting, not keyword labels. Make readers want to continue.
- Take a clear position. Don't hedge with both sides.
- End with a specific action the reader can do in the next 24 hours.

SEO RULES:
- Primary keyword in title, first paragraph, one H2, and meta description. That's it.
- Title: human-first, SEO-second.
- Meta description: compelling movie-trailer hook, not keyword summary. Max 155 characters.
- No keyword stuffing. If the keyword appears more than 5 times in 1,000 words, you've overdone it.
- Headings are written for readers, not crawlers.

=== BRAND CONTEXT ===
Brand: __BRAND_NAME__
Industry: __INDUSTRY__
Products/Services: __PRODUCTS__
Location: __LOCATION__
Target audience: __AUDIENCE__

=== BLOG BRIEF ===
Topic: __TOPIC__
Primary keyword: __KEYWORD__
Additional SEO keywords: __SECONDARY_KEYWORDS__

__STRUCTURE_BLOCK__

Write the blog now. Output ONLY the blog content in Markdown format.
Start with exactly these two labelled lines, then the blog:

TITLE: [your suggested title here]
META: [your meta description here, max 155 characters]

Then the full blog in Markdown. Do not add any other preamble."""


def _build_blog_prompt(
    writing_dna_prompt: str,
    brand_name: str,
    industry: str,
    products: str,
    location: str,
    audience: str,
    topic: str,
    keyword: str,
    word_count: int,
    secondary_keywords: List[str],
) -> str:
    secondary = ", ".join(secondary_keywords) if secondary_keywords else "none"

    dna_fallback = (
        "Write with authority, clarity, and specificity. "
        "Use real examples and numbers. Vary sentence length. "
        "Sound like a thoughtful expert, not a content mill."
    )

    # Calculate mandatory section structure so the model can't stop early.
    # Each body section gets a minimum word target; the model must hit it before moving on.
    num_body_sections = max(4, word_count // 350)       # 4 sections for 1400+, 5 for 1750+, etc.
    intro_words     = max(120, word_count // 10)        # ~10% for intro
    conclusion_words = max(100, word_count // 12)       # ~8% for conclusion
    body_budget     = word_count - intro_words - conclusion_words
    per_section_min = body_budget // num_body_sections

    section_reqs = "\n".join(
        f"  - Body section {i+1}: minimum {per_section_min} words"
        for i in range(num_body_sections)
    )

    structure_block = f"""=== MANDATORY STRUCTURE AND WORD COUNT ===
Total target: {word_count} words. This is a hard minimum — not a suggestion.

Required sections and minimum word counts:
  - Introduction: minimum {intro_words} words
{section_reqs}
  - Closing / call-to-action: minimum {conclusion_words} words

Rules for hitting the word count:
1. Write the Introduction first ({intro_words}+ words). Only move to body sections when done.
2. Write each body section fully ({per_section_min}+ words each) before starting the next.
   Expand with: a specific real example, a number or statistic, a story, or practical how-to steps.
3. DO NOT start the closing section until you have written at least {word_count - conclusion_words} words.
4. After the closing, check: if total body is under {word_count} words, go back and expand the shortest body section.

Every section must stand on its own — no thin paragraphs, no one-liner sections.
A 2000-word request that delivers 800 words is a failure. Write to the number."""

    return (
        _BLOG_SYSTEM_PROMPT_TEMPLATE
        .replace("__WRITING_DNA_PROMPT__", writing_dna_prompt or dna_fallback)
        .replace("__BRAND_NAME__", brand_name or "the brand")
        .replace("__INDUSTRY__", industry or "business")
        .replace("__PRODUCTS__", products or "products and services")
        .replace("__LOCATION__", location or "Nigeria")
        .replace("__AUDIENCE__", audience or "business owners and entrepreneurs")
        .replace("__TOPIC__", topic)
        .replace("__KEYWORD__", keyword)
        .replace("__WORD_COUNT__", str(word_count))
        .replace("__SECONDARY_KEYWORDS__", secondary)
        .replace("__STRUCTURE_BLOCK__", structure_block)
    )


def _parse_blog_output(raw: str) -> Dict[str, str]:
    """Extract TITLE:, META:, and body from raw AI output."""
    title = ""
    meta = ""
    body_lines = []

    lines = raw.strip().splitlines()
    header_done = False
    for line in lines:
        stripped = line.strip()
        if not header_done:
            if stripped.upper().startswith("TITLE:"):
                title = stripped[6:].strip()
                continue
            if stripped.upper().startswith("META:"):
                meta = stripped[5:].strip()
                continue
            if title and meta:
                header_done = True
        body_lines.append(line)

    # If header wasn't cleanly separated, fall back to searching the whole output
    if not title:
        for line in lines:
            m = re.match(r"^TITLE:\s*(.+)$", line, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                break
    if not meta:
        for line in lines:
            m = re.match(r"^META:\s*(.+)$", line, re.IGNORECASE)
            if m:
                meta = m.group(1).strip()
                break

    # Strip TITLE/META lines from body
    body = "\n".join(
        ln for ln in body_lines
        if not re.match(r"^(TITLE:|META:)", ln.strip(), re.IGNORECASE)
    ).strip()

    return {"title": title, "meta": meta, "body": body}


class BlogGenerationService:
    """Generates blog posts using the user's Writing DNA voice profile."""

    @staticmethod
    async def generate(
        user_id: str,
        topic: str,
        primary_keyword: str,
        secondary_keywords: List[str],
        word_count: int,
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """
        Generate a blog post that sounds like the brand owner wrote it.
        Fetches Writing DNA + brand profile from DB, injects into the system prompt.
        """

        # Fetch Writing DNA (optional — generation works without it)
        dna_prompt = await WritingDNAService.get_prompt(user_id, db)

        # Fetch brand profile for context
        brand_ctx: Dict[str, Any] = {}
        raw_profile = await db["brand_profiles"].find_one({"user_id": user_id})
        if raw_profile:
            raw_profile.pop("_id", None)
            brand_ctx = BrandProfileService.to_brand_context(raw_profile)

        brand_name = brand_ctx.get("brand_name", "")
        industry = brand_ctx.get("industry", "")
        products = ", ".join(brand_ctx.get("key_products_services", [])[:5])
        location = brand_ctx.get("region", "Nigeria")
        audience = brand_ctx.get("target_audience", "")

        prompt = _build_blog_prompt(
            writing_dna_prompt=dna_prompt or "",
            brand_name=brand_name,
            industry=industry,
            products=products,
            location=location,
            audience=audience,
            topic=topic,
            keyword=primary_keyword,
            word_count=word_count,
            secondary_keywords=secondary_keywords,
        )

        # max_tokens = word_count * 2 gives ample headroom (1 word ≈ 1.3 tokens).
        # Cap at 8000 to stay within gpt-4o's output limit.
        max_tokens = min(word_count * 2, 8000)

        ai_request = AIService.build_ai_model(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o",
            temperature=0.75,
        )
        ai_request.max_tokens = max_tokens

        ai_response = await AIService.chat_completion(ai_request)

        if isinstance(ai_response, dict) and "error" in ai_response:
            return UriResponse.error_response(ai_response["error"])

        raw_output = ai_response.choices[0].message.content.strip()

        # ── Two-pass expansion ───────────────────────────────────────────────
        # If the model stopped more than 10% short, send a continuation call
        # that asks it to rewrite the blog with the existing content expanded.
        actual_words = len(raw_output.split())
        threshold = int(word_count * 0.90)

        if actual_words < threshold:
            shortage = word_count - actual_words
            print(f"📝 Blog is {actual_words} words (target {word_count}). Running expansion pass (+{shortage} words needed).")

            expansion_prompt = (
                f"The blog below is {actual_words} words. It needs to be at least {word_count} words. "
                f"Rewrite it in full, keeping the same title, meta description, voice, structure, and Writing DNA. "
                f"Expand body sections by adding: more specific examples with real numbers, deeper explanations, "
                f"short stories or case studies, and practical step-by-step guidance. "
                f"Do not add a new conclusion — make existing sections richer. "
                f"Output the complete rewritten blog starting with TITLE: and META: exactly as before.\n\n"
                f"CURRENT BLOG TO EXPAND:\n{raw_output}"
            )

            exp_request = AIService.build_ai_model(
                messages=[{"role": "user", "content": expansion_prompt}],
                model="gpt-4o",
                temperature=0.7,
            )
            exp_request.max_tokens = min(word_count * 3, 8000)

            exp_response = await AIService.chat_completion(exp_request)
            if not (isinstance(exp_response, dict) and "error" in exp_response):
                expanded = exp_response.choices[0].message.content.strip()
                expanded_words = len(expanded.split())
                if expanded_words > actual_words:
                    raw_output = expanded
                    print(f"✅ Expansion complete: {expanded_words} words (was {actual_words})")
                else:
                    print(f"⚠️ Expansion didn't help ({expanded_words} vs {actual_words}), using original")

        parsed = _parse_blog_output(raw_output)

        blog_id = str(ObjectId())
        now = datetime.utcnow()

        doc = {
            "id": blog_id,
            "user_id": user_id,
            "topic": topic,
            "primary_keyword": primary_keyword,
            "secondary_keywords": secondary_keywords,
            "target_word_count": word_count,
            "generated_content": parsed["body"],
            "generated_title": parsed["title"],
            "generated_meta": parsed["meta"],
            "current_content": parsed["body"],
            "current_title": parsed["title"],
            "edit_history": [],
            "feedback": {},
            "status": "draft",
            "brand_context_snapshot": {
                "brand_name": brand_name,
                "industry": industry,
                "location": location,
            },
            "has_writing_dna": bool(dna_prompt),
            "created_at": now,
            "updated_at": now,
        }

        await db[BLOG_POSTS_COLLECTION].insert_one(doc)
        doc.pop("_id", None)

        word_count_actual = len(parsed["body"].split())
        print(
            f"✅ Blog generated: id={blog_id}, words={word_count_actual}, "
            f"dna={'yes' if dna_prompt else 'no'}, title={parsed['title'][:60]}"
        )

        return UriResponse.create_response("blog_post", {
            "blog_id": blog_id,
            "title": parsed["title"],
            "meta": parsed["meta"],
            "content": parsed["body"],
            "word_count": word_count_actual,
            "has_writing_dna": bool(dna_prompt),
            "status": "draft",
            "created_at": now.isoformat(),
        })

    @staticmethod
    async def list_posts(user_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        cursor = db[BLOG_POSTS_COLLECTION].find(
            {"user_id": user_id},
            {"_id": 0, "generated_content": 0, "current_content": 0},
        ).sort("created_at", -1).limit(50)
        posts = await cursor.to_list(length=50)
        return UriResponse.get_list_data_response("blog_post", posts)

    @staticmethod
    async def get_post(blog_id: str, user_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        doc = await db[BLOG_POSTS_COLLECTION].find_one(
            {"id": blog_id, "user_id": user_id}
        )
        if not doc:
            return UriResponse.get_single_data_response("blog_post", None)
        doc.pop("_id", None)
        return UriResponse.get_single_data_response("blog_post", doc)

    @staticmethod
    async def update_post(
        blog_id: str,
        user_id: str,
        new_content: str,
        new_title: Optional[str],
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """
        Save user edits. Appends a snapshot to edit_history so the learning
        pipeline can diff original vs edited.
        """
        doc = await db[BLOG_POSTS_COLLECTION].find_one({"id": blog_id, "user_id": user_id})
        if not doc:
            return UriResponse.get_single_data_response("blog_post", None)

        history_entry = {
            "content_before": doc.get("current_content", ""),
            "title_before": doc.get("current_title", ""),
            "edited_at": datetime.utcnow(),
        }

        update: Dict[str, Any] = {
            "current_content": new_content,
            "updated_at": datetime.utcnow(),
        }
        if new_title:
            update["current_title"] = new_title

        await db[BLOG_POSTS_COLLECTION].update_one(
            {"id": blog_id},
            {
                "$set": update,
                "$push": {"edit_history": history_entry},
            },
        )

        updated = await db[BLOG_POSTS_COLLECTION].find_one({"id": blog_id})
        if updated:
            updated.pop("_id", None)
        return UriResponse.update_response("blog_post", updated)

    @staticmethod
    async def record_feedback(
        blog_id: str,
        user_id: str,
        rating: str,
        issues: Optional[List[str]],
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """Record thumbs-up / thumbs-down feedback."""
        result = await db[BLOG_POSTS_COLLECTION].update_one(
            {"id": blog_id, "user_id": user_id},
            {
                "$set": {
                    "feedback": {
                        "rating": rating,
                        "issues": issues or [],
                        "recorded_at": datetime.utcnow(),
                    },
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        if result.matched_count == 0:
            return UriResponse.get_single_data_response("blog_post", None)

        print(f"📊 Blog feedback recorded: id={blog_id}, rating={rating}, issues={issues}")
        return UriResponse.update_response(
            "blog_post",
            {"blog_id": blog_id, "rating": rating, "issues": issues or []},
            "Feedback recorded.",
        )

    @staticmethod
    async def publish_post(
        blog_id: str,
        user_id: str,
        published_url: Optional[str],
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        update: Dict[str, Any] = {
            "status": "published",
            "published_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if published_url:
            update["published_url"] = published_url

        result = await db[BLOG_POSTS_COLLECTION].update_one(
            {"id": blog_id, "user_id": user_id},
            {"$set": update},
        )
        if result.matched_count == 0:
            return UriResponse.get_single_data_response("blog_post", None)

        return UriResponse.update_response(
            "blog_post",
            {"blog_id": blog_id, "status": "published"},
            "Blog post published.",
        )
