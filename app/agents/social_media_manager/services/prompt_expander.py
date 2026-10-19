"""
Prompt expander for content creation.

Account managers were writing better prompts in ChatGPT and pasting them into
Jane, because a short idea ("happy Mother's Day post") gave weak content.
This turns a short idea into a fuller creative brief *before* generation,
guided by the brand's own Playbook and by everything chosen for this post.
The owner sees the brief, can edit or undo it, and only then generates.

How the brief is used downstream shapes how it is written:
  • The text generator receives the whole brief as the post's subject.
  • Image generation mostly reads only the START of it (e.g. "This image
    represents: <first 300 characters>"). So the brief always OPENS with a
    concept sentence: the occasion/subject, the core message and the key
    visual — never filler like "Jane, create a post…".
  • Every post gets a visual. The images switch on the form sits below the
    prompt box and is usually turned on after writing the prompt, so the
    brief always includes visual direction and never calls a post text-only.

Rules: keep the owner's idea central; never invent products, prices, offers,
discounts, dates, statistics or results; no medical or guaranteed-result
claims; respect words to avoid and guardrails; greeting posts celebrate the
occasion with the brand as the warm sender — no pitch, no analogy between the
occasion and the product.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.services.AIService import AIService

PROMPT_VERSION = "content-prompt-expander-v3"
MODEL = "gpt-4o"
MAX_CHARS = 1800
CONCEPT_MAX = 280


class _Expanded(BaseModel):
    concept: str = Field(description="ONE sentence (max ~40 words): the occasion/subject, the core message and the "
                                     "key visual scene. This is what the image is generated from.")
    details: str = Field(description="The rest of the brief as plain instructions (100-180 words).")
    used: list[str] = Field(default_factory=list, description="Which brand facts or settings shaped it, 2-4 words each.")


SYSTEM = """You turn a short content idea into a clear, specific creative brief for Jane, an AI that writes a social
post AND generates its image(s) for this brand. Write what an excellent social media manager would write.

Return two parts:
1. concept — ONE sentence that opens the brief: the occasion or subject, the core message, and the key visual
   scene (who/what is in the picture, setting, mood). Image generation reads mainly this sentence, so make the
   visual concrete. Do not start with "Jane", "Create", "Craft" or "Write".
2. details — plain instructions (no headings, markdown or bullet symbols), covering only what helps:
   - the angle/hook and the key message, who it speaks to (from the audience facts)
   - tone and voice (from the brand voice and the platform tone), words to avoid
   - visual direction: the chosen visual style(s), brand colours, setting and people appropriate to the brand's
     market. EVERY post has an image — never call the post text-only or say images are off.
   - uploaded photos: if photos are listed, say how to feature them (they are real products/people — use, don't
     reinvent them)
   - format notes for the chosen post type (carousel: a short slide-by-slide flow; story: very brief text)
   - the call to action: if a CTA is given, use it exactly; otherwise match the brand's goal and CTA style

Hard rules:
- Keep the owner's idea and intent central. If the idea is already detailed, refine it lightly — don't rewrite it.
- Use ONLY the brand facts given. Never invent products, prices, offers, discounts, events, dates, statistics or
  results. No medical, financial or guaranteed-result claims. Respect the guardrails.
- Greeting / occasion posts (Mother's Day, Independence Day, Eid, Christmas, New Year…): celebrate the occasion and
  the people; the brand is simply the warm sender, signing off at most. Never compare the occasion to the product
  or the brand ("just like a mother nurtures…, we nurture…") and no sales message.
- Vague ideas ("tips post", "engagement post"): choose ONE specific, useful angle from the customers' pain points,
  desires or common questions, and name it concretely.
- Occasions: culturally accurate and respectful; never state a date unless the owner gave it.
- Don't write the post itself — write the brief for it. Don't list hashtags; Jane adds them."""


def _style_lines(slugs: list[str]) -> list[str]:
    try:
        from app.agents.social_media_manager.services.v3_style_library import V3_STYLES
    except Exception:
        V3_STYLES = {}
    out = []
    for s in slugs[:3]:
        st = V3_STYLES.get(s)
        out.append(f"{st.name}: {st.description}" if st else str(s).replace("_", " "))
    return out


def _platform_tones(bc: dict, platforms: list[str]) -> list[str]:
    tones = bc.get("platform_tones") or {}
    if bc.get("same_tone_everywhere", True) or not isinstance(tones, dict):
        return []
    return [f"{p}: {tones[p]}" for p in platforms if tones.get(p)]


def brand_facts(bc: dict, styles: list[str], platforms: Optional[list[str]] = None) -> dict:
    """Only the Playbook fields that make a brief better, empty ones dropped."""
    clean = lambda xs, n=6: [str(x).strip() for x in (xs or []) if str(x or "").strip()][:n]
    guard = bc.get("guardrails") or {}
    facts = {
        "brand": bc.get("brand_name"),
        "industry": bc.get("industry"),
        "what we sell": bc.get("business_description"),
        "products/services": clean(bc.get("key_products_services"), 8),
        "what makes us different": bc.get("unique_selling_proposition") or bc.get("why_customers_choose_us"),
        "price range": bc.get("price_range"),
        "audience": bc.get("target_audience") or bc.get("ideal_customer_profile"),
        "customer location": bc.get("customer_location") or bc.get("region"),
        "customer desires": clean(bc.get("customer_desires") or bc.get("customer_needs")),
        "customer pain points": clean(bc.get("customer_pain_points")),
        "common questions": clean(bc.get("common_questions"), 4),
        "brand voice": bc.get("brand_voice"),
        "platform tone": _platform_tones(bc, platforms or []),
        "voice sample": (bc.get("voice_sample") or "")[:300],
        "words to avoid": clean(bc.get("words_to_avoid"), 12),
        "guardrails": "; ".join(f"{k}: {v}" for k, v in guard.items() if v) if isinstance(guard, dict) else str(guard),
        "primary goal": bc.get("primary_goal"),
        "CTA style": clean(bc.get("cta_styles"), 3),
        "default link": bc.get("default_link"),
        "brand colours": clean(bc.get("brand_colors"), 5),
        "visual style": _style_lines(styles),
        "content pillars": clean(bc.get("content_pillars")),
        "brand's own key dates": (bc.get("key_dates") or "")[:300] if isinstance(bc.get("key_dates"), str) else "",
        "languages": clean(bc.get("languages"), 3),
    }
    return {k: v for k, v in facts.items() if v}


def build_messages(idea: str, bc: dict, platforms: list[str], post_type: str, num_slides: Optional[int],
                   styles: list[str], today: datetime, cta: Optional[str] = None,
                   photos: Optional[list[str]] = None) -> list[dict]:
    facts = brand_facts(bc, styles, platforms)
    facts_text = "\n".join(f"- {k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in facts.items())
    post = post_type + (f" with {num_slides} slides" if post_type == "carousel" and num_slides else "")
    lines = [f"Platforms: {', '.join(platforms) or 'not chosen yet'}", f"Post type: {post}",
             "Visual: every post gets an image (the images switch is set after this brief)",
             f"Today: {today.strftime('%A %d %B %Y')}"]
    if cta:
        lines.append(f"Call to action to use exactly: {cta}")
    if photos:
        lines.append(f"Uploaded photos to feature ({len(photos)}): " + "; ".join(p or "unlabelled photo" for p in photos))
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"BRAND FACTS\n{facts_text or '- (the Playbook is mostly empty)'}\n\n"
                                    f"POST SETTINGS\n" + "\n".join(lines) +
                                    f"\n\nOWNER'S IDEA\n{idea.strip()}\n\nWrite the brief."},
    ]


_LEAD_IN = re.compile(r"^(jane[,:]?\s*)?(please\s+)?(create|craft|write|make|design|generate)\s+(a|an)\s+", re.I)


def _clean(text: str) -> str:
    text = (text or "").replace("**", "")
    text = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", r"\1", text)  # markdown links -> plain text
    text = re.sub(r"(?i)\b(this|the) post is text[- ]only[^.]*\.\s*", "", text)  # never "text-only"
    return " ".join(text.split())


def compose(concept: str, details: str) -> str:
    """Concept sentence first (what image generation reads), then the details."""
    concept = _clean(concept)
    concept = _LEAD_IN.sub("", concept)
    concept = concept[:1].upper() + concept[1:] if concept else concept
    if len(concept) > CONCEPT_MAX:
        cut = concept[:CONCEPT_MAX]
        concept = cut[: cut.rfind(" ")].rstrip(",;:") + "."
    if concept and concept[-1] not in ".!?":
        concept += "."
    text = f"{concept} {_clean(details)}".strip()
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        text = cut[: cut.rfind(".") + 1] or cut
    return text


async def expand(idea: str, bc: dict, platforms: list[str], post_type: str = "feed", num_slides: Optional[int] = None,
                 styles: Optional[list[str]] = None, cta: Optional[str] = None,
                 photos: Optional[list[str]] = None) -> Optional[dict]:
    """{"expanded_prompt", "used"} or None if the AI call failed."""
    styles = styles or list(bc.get("style_selections") or [])
    today = datetime.now(timezone(timedelta(hours=1)))  # Lagos time
    req = AIService.build_ai_model(model=MODEL, temperature=0.6,
                                   messages=build_messages(idea, bc, platforms, post_type, num_slides, styles,
                                                           today, cta, photos))
    try:
        completion = await AIService.structured_chat_completion(req, response_model=_Expanded)
        if isinstance(completion, dict) and "error" in completion:
            print(f"[PromptExpander] failed: {completion['error']}")
            return None
        parsed: Optional[_Expanded] = completion.choices[0].message.parsed
    except Exception as e:
        print(f"[PromptExpander] raised: {e}")
        return None
    if not parsed or not (parsed.concept.strip() or parsed.details.strip()):
        return None
    return {"expanded_prompt": compose(parsed.concept, parsed.details), "used": parsed.used[:6],
            "prompt_version": PROMPT_VERSION}
