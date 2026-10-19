"""
Prompt expander for content creation.

Account managers were writing better prompts in ChatGPT and pasting them into
Jane, because a short idea ("happy Mother's Day post") gave weak content.
This turns a short idea into a fuller creative brief *before* generation,
guided by the brand's own Playbook — offer, audience, voice, words to avoid,
guardrails, goal, CTA style — and by the visual style and post settings
chosen for this post. The owner sees the expanded prompt, can edit or undo
it, and only then generates; nothing is hidden or sent automatically.

Rules the expansion follows: keep the owner's idea at the centre; never
invent products, prices, offers, discounts or results; no medical or
guaranteed-result claims; respect words to avoid and guardrails; be
culturally accurate for Nigerian and other occasions, without guessing dates.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.services.AIService import AIService

PROMPT_VERSION = "content-prompt-expander-v1"
MODEL = "gpt-4o"
MAX_CHARS = 1800


class _Expanded(BaseModel):
    expanded_prompt: str = Field(description="The expanded creative brief, written as an instruction to Jane.")
    used: list[str] = Field(default_factory=list, description="Which brand facts shaped it, in 2-4 words each.")


SYSTEM = """You turn a short content idea into a clear, specific creative brief for Jane, an AI that writes social
posts (and images) for this brand. The brief is what an excellent social media manager would write.

Write the brief as direct instructions to Jane, in plain sentences (no headings, no markdown, no bullet symbols),
120-220 words, covering only what helps:
- the goal of the post and the angle/hook that fits this brand and occasion
- who it speaks to (from the audience facts)
- the key message, and how it connects naturally to what the brand sells (only if it fits the occasion; a
  greeting post should not become a hard sell)
- tone and voice (from the brand voice), plus any words to avoid
- visual direction for the image(s), using the chosen visual style(s) and brand colours, when images are on
- format notes for the platforms and post type (e.g. carousel slide flow, story brevity)
- the call to action, matching the brand's goal and CTA style

Hard rules:
- Keep the owner's idea and intent central. If the idea is already detailed, refine it lightly — don't rewrite it.
- Use ONLY the brand facts given. Never invent products, prices, offers, discounts, events, statistics or results.
- No medical, financial or guaranteed-result claims. Respect the guardrails.
- Occasions: be culturally accurate (e.g. Nigerian Independence Day, Mothers' Day, Sallah, Christmas) and
  respectful; never state a date unless the owner gave it.
- Don't write the post itself — write the brief for it."""


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


def brand_facts(bc: dict, styles: list[str]) -> dict:
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
        "brand voice": bc.get("brand_voice"),
        "voice sample": (bc.get("voice_sample") or "")[:300],
        "words to avoid": clean(bc.get("words_to_avoid"), 12),
        "guardrails": "; ".join(f"{k}: {v}" for k, v in guard.items() if v) if isinstance(guard, dict) else str(guard),
        "primary goal": bc.get("primary_goal"),
        "CTA style": clean(bc.get("cta_styles"), 3),
        "default link": bc.get("default_link"),
        "brand colours": clean(bc.get("brand_colors"), 5),
        "visual style": _style_lines(styles),
        "content pillars": clean(bc.get("content_pillars")),
        "languages": clean(bc.get("languages"), 3),
    }
    return {k: v for k, v in facts.items() if v}


def build_messages(idea: str, bc: dict, platforms: list[str], post_type: str, num_slides: Optional[int],
                   include_images: bool, styles: list[str], today: datetime) -> list[dict]:
    facts = brand_facts(bc, styles if include_images else [])
    facts_text = "\n".join(f"- {k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in facts.items())
    post = f"{post_type}" + (f" with {num_slides} slides" if post_type == "carousel" and num_slides else "")
    settings = (f"Platforms: {', '.join(platforms) or 'not chosen'}\nPost type: {post}\n"
                f"Images: {'yes' if include_images else 'no (text only)'}\nToday: {today.strftime('%A %d %B %Y')}")
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"BRAND FACTS\n{facts_text or '- (the Playbook is mostly empty)'}\n\n"
                                    f"POST SETTINGS\n{settings}\n\nOWNER'S IDEA\n{idea.strip()}\n\nWrite the brief."},
    ]


async def expand(idea: str, bc: dict, platforms: list[str], post_type: str = "feed", num_slides: Optional[int] = None,
                 include_images: bool = False, styles: Optional[list[str]] = None) -> Optional[dict]:
    """{"expanded_prompt", "used"} or None if the AI call failed."""
    styles = styles or list(bc.get("style_selections") or [])
    today = datetime.now(timezone(timedelta(hours=1)))  # Lagos time
    req = AIService.build_ai_model(model=MODEL, temperature=0.6,
                                   messages=build_messages(idea, bc, platforms, post_type, num_slides,
                                                           include_images, styles, today))
    try:
        completion = await AIService.structured_chat_completion(req, response_model=_Expanded)
        if isinstance(completion, dict) and "error" in completion:
            print(f"[PromptExpander] failed: {completion['error']}")
            return None
        parsed: Optional[_Expanded] = completion.choices[0].message.parsed
    except Exception as e:
        print(f"[PromptExpander] raised: {e}")
        return None
    if not parsed or not parsed.expanded_prompt.strip():
        return None
    text = " ".join(parsed.expanded_prompt.replace("**", "").split())
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        text = cut[: cut.rfind(".") + 1] or cut
    return {"expanded_prompt": text, "used": parsed.used[:6], "prompt_version": PROMPT_VERSION}
