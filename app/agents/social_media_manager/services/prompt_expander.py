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

PROMPT_VERSION = "content-prompt-expander-v4"
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
2. details — plain instructions that continue from the concept without repeating it (no headings, markdown or
   bullet symbols), covering only what helps:
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


# The "Describe it" video flow's own brief box (DescribeVideoGenerator.tsx). A video brief is not a paragraph: the
# script writer, the picture model and the video model each need different things from it, and a loose paragraph left
# them guessing (faces that changed between scenes, brand colour washed over every scene, lines the speaker never said).
# So this one is STRUCTURED: fixed fields the model fills in, laid out under fixed headings by compose_video() below.
# The structure is exactly what the video pipeline already rewards (see video_creative_service.py): people described once
# and repeated, one scene per ~5 seconds, exact spoken lines, short captions, brand colours as accents.
# The 12 video styles the script writer can follow (VIDEO_STYLE_DIRECTIVES in video_storyboard_service.py has the full
# directive for each; a test keeps these slugs in step). Kept here as short gists so this module does not import the
# heavy storyboard service. The expander picks ONE for the idea and names it in the brief, so the script writer follows it.
VIDEO_STYLES: dict[str, tuple[str, str]] = {
    "clean_commercial": ("Clean Commercial", "polished brand commercial: smooth dolly moves, hero product shots, clean cuts"),
    "luxury_slow_burn": ("Luxury Slow Burn", "premium and cinematic: macro close-ups, very slow push-ins, long unhurried scenes"),
    "viral_fast_cut": ("Viral Fast Cut", "high-energy and social-native: handheld, snap zooms, very fast cuts, an instant hook"),
    "ingredient_reveal": ("Ingredient Reveal", "a sensory reveal of raw ingredients in macro texture, building to the hero product"),
    "street_style": ("Street Style", "authentic urban documentary: handheld, natural rhythm, real-life moments"),
    "unboxing_drama": ("Unboxing Drama", "a theatrical build-up and reveal of a product and its packaging, hands in close-up"),
    "before_after": ("Before / After", "a problem then its transformation: matched angles, muted first half, bright second half"),
    "mood_film": ("Mood Film", "atmosphere and emotion over product information: wide, slow, golden-hour feel"),
    "product_explosion": ("Product Explosion", "dynamic, physics-defying product visuals: orbits, particles, freeze-frames"),
    "testimonial_style": ("Testimonial Style", "an authentic person-to-camera testimonial or interview, conversational pacing"),
    "menu_showcase": ("Menu Showcase", "food that looks irresistible: overhead and glamour angles, lingering on texture"),
    "countdown_hype": ("Countdown Hype", "urgency that escalates scene by scene: each scene tighter and faster than the last"),
}


def _style_menu() -> str:
    return "\n".join(f"    {slug}: {gist}" for slug, (_name, gist) in VIDEO_STYLES.items())


VIDEO_SYSTEM = """You turn a short video idea into a structured production brief for Jane, an AI that writes a
short-form video script and then generates the pictures and footage, scene by scene, for this brand. Write what an
excellent video director would hand to a crew. Fill every field below; the app lays them out under fixed headings.

- concept: ONE sentence: the core story or message and the opening picture (who or what is on screen first, where,
  the mood). Do not start with "Jane", "Create", "Craft" or "Write".
- style: the ONE video style that best fits the idea, as its slug. If the owner names a style, use it. The script
  writer follows the style you name for camera, pacing and energy:
{STYLE_MENU}
- people: ONLY if people appear. Many styles are about the product, with no face at all (or only hands): then leave it
  empty. At most two. One entry per person: approximate age, gender, skin tone, face shape,
  hair (style, length, colour), facial hair, build, and ONE fixed outfit with its colours, fitting the brand's market.
  Every scene is drawn separately, so a person described once here must look identical in every scene: never
  change their outfit or look between scenes. Empty if no one is on screen.
- setting: where and when (one place, or a clear progression), time of day, key props, how it is lit. Realistic and
  culturally accurate for the brand's market. Packaging, uniforms, signs and screens are plain and unbranded, except
  the brand's own name.
- look: framing, camera movement and colour, written in the chosen style's own terms (its camera, energy and colour
  grading). A fast or dramatic style gets fast, dramatic CAMERA and PRODUCT movement: snap zooms, orbits, quick cuts,
  freeze-frames. But whenever a person's face is in shot it stays large, sharp and well lit (medium shot or
  close-up), and the person moves gently (small gestures, a slow push-in): never fast spins, whip-pans, running or
  crowds of faces on a person, which distort. Put the energy in the camera and the product, not in a face. Brand
  colours go on the logo, product, packaging, graphics and on-screen text; the rest of every scene (skin, food, sky,
  rooms, backgrounds) keeps natural colours, so scenes do not all look dyed.
- scenes: EXACTLY the number of scenes you are told, in order, one per 5 seconds. Scene 1 is the hook, the middle
  builds or shows, the last pays off and carries the call to action. Each scene continues straight from the one
  before (same people, same place). Per scene:
    action: what happens on screen, concrete and visual, one or two sentences (about 25 words at most).
    says: the exact words a person speaks to camera in this scene, or empty. Use this ONLY when the idea calls for
      someone speaking to camera (a testimonial, a founder, a how-to). Then write ONE continuous spoken script
      split across the scenes like one take cut into pieces, in natural spoken language (a Lagos customer sounds
      like one), short enough to say in 5 seconds (about 12 words at most). No spoken lines if nobody speaks.
    caption: the on-screen text for this scene, 8 words or fewer, or empty. It is drawn onto the video afterwards.
- sound: the voice (tone, and accent if the market implies one), the music style or none, the key sound effects.
  Never name a copyrighted song.
- call_to_action: if a CTA is given, use it exactly; otherwise match the brand's goal. Say where it appears (the
  last scene's caption, spoken, or both).
- avoid: the brand's words to avoid and guardrails, and anything else this video must not show or say. Empty if none.
- used: which brand facts or settings shaped it, 2-4 words each.

Hard rules:
- Keep the owner's idea and intent central. If the idea is already detailed or already has scenes and spoken lines,
  refine it lightly: keep their scenes and keep every line they wrote in quotes WORD FOR WORD.
- Use ONLY the brand facts given. Never invent products, prices, offers, discounts, events, dates, statistics or
  results. No medical, financial or guaranteed-result claims. Respect the guardrails.
- Greeting / occasion videos (Mother's Day, Independence Day, Eid, Christmas, New Year…): celebrate the occasion and
  the people; the brand is simply the warm sender, signing off at most. Never compare the occasion to the product or
  the brand and no sales message.
- Vague ideas ("tips video", "engagement video"): choose ONE specific, useful angle from the customers' pain points,
  desires or common questions, and name it concretely.
- Occasions: culturally accurate and respectful; never state a date unless the owner gave it.
- Plain text only: no markdown, no headings, no bullet symbols inside the fields.
- Don't write the final script, write the brief for it."""


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


class _VideoScene(BaseModel):
    action: str = Field(description="What happens on screen: concrete and visual, one or two sentences.")
    says: str = Field(default="", description="The exact words spoken to camera in this scene, or empty.")
    caption: str = Field(default="", description="On-screen text for this scene, 8 words or fewer, or empty.")


class _ExpandedVideo(BaseModel):
    concept: str = Field(description="ONE sentence: the core story and the opening picture.")
    style: str = Field(default="", description="The slug of the ONE video style that fits the idea.")
    people: list[str] = Field(default_factory=list, description="One fixed description per person on screen (max 2).")
    setting: str = Field(default="", description="Where, when, key props, how it is lit.")
    look: str = Field(default="", description="Framing, gentle movement, and where brand colours go.")
    scenes: list[_VideoScene] = Field(default_factory=list, description="Exactly the number of scenes asked for.")
    sound: str = Field(default="", description="Voice, music, sound effects.")
    call_to_action: str = Field(default="", description="The call to action and where it appears.")
    avoid: str = Field(default="", description="Words and things this video must avoid.")
    used: list[str] = Field(default_factory=list, description="Which brand facts shaped it, 2-4 words each.")


VIDEO_PROMPT_VERSION = "video-prompt-expander-v3"
VIDEO_MAX_CHARS = 1900            # the brief box holds 2000
SECONDS_PER_SCENE = 5             # the pipeline cuts a script into scenes of about this length
CAPTION_MAX_WORDS = 8
_ONE_LINE = re.compile(r"\s+")


def scene_count(duration_seconds: Optional[int]) -> int:
    """How many scenes the video pipeline will cut this length into (the same sum the script writer uses)."""
    return max(1, round((duration_seconds or 15) / SECONDS_PER_SCENE))


def _line(text: Any) -> str:
    """One tidy line of plain text: no markdown, no line breaks."""
    return _ONE_LINE.sub(" ", _clean(str(text or ""))).strip()


def _sentence(text: Any) -> str:
    t = _line(text)
    return t if not t or t[-1] in ".!?" else t + "."


def _scene_line(index: int, total: int, duration_seconds: Optional[int], scene: Any, action_max: Optional[int] = None) -> str:
    start = index * SECONDS_PER_SCENE
    end = (index + 1) * SECONDS_PER_SCENE
    if index == total - 1 and duration_seconds:
        end = max(end, int(duration_seconds))
    action = _sentence(getattr(scene, "action", ""))
    if action_max and len(action) > action_max:
        cut = action[:action_max]
        action = (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(",;:") + "."
    parts = [f"SCENE {index + 1} ({start}-{end}s): {action}"]
    says = _line(getattr(scene, "says", "")).replace('"', "'")
    if says:
        parts.append(f'Says exactly: "{says}"')
    caption = " ".join(_line(getattr(scene, "caption", "")).replace('"', "'").split()[:CAPTION_MAX_WORDS])
    if caption:
        parts.append(f'Caption: "{caption}"')
    return " ".join(parts)


def compose_video(parsed: Any, duration_seconds: Optional[int] = None) -> str:
    """The structured fields laid out under fixed headings, one per line, for the video brief box:

        CONCEPT: ...
        STYLE: Viral Fast Cut.
        PERSON 1: ...            (only if people appear)
        SETTING: ...
        LOOK: ...
        SCENE 1 (0-5s): action. Says exactly: "..." Caption: "..."
        ...
        SOUND: ...
        CLOSE: ...
        AVOID: ...

    Fits the 2000-character box: the optional lines go first (avoid, sound, look, setting), then each scene's action is
    shortened, but a spoken line is never cut."""
    total = scene_count(duration_seconds)
    scenes = list(getattr(parsed, "scenes", None) or [])[:total]
    concept = _sentence(_LEAD_IN.sub("", _line(getattr(parsed, "concept", ""))))
    if concept:
        concept = concept[:1].upper() + concept[1:]
    people = [_sentence(p) for p in (getattr(parsed, "people", None) or []) if _line(p)][:2]
    style_name = (VIDEO_STYLES.get(_line(getattr(parsed, "style", "")).lower()) or ("",))[0]   # an unknown slug is dropped

    def build(drop: tuple = (), action_max: Optional[int] = None) -> str:
        lines = []
        if concept:
            lines.append(f"CONCEPT: {concept}")
        if style_name:
            lines.append(f"STYLE: {style_name}.")
        for i, person in enumerate(people):
            lines.append(f"PERSON {i + 1}: {person}" if len(people) > 1 else f"PERSON: {person}")
        for key, label in (("setting", "SETTING"), ("look", "LOOK")):
            text = _sentence(getattr(parsed, key, ""))
            if text and key not in drop:
                lines.append(f"{label}: {text}")
        for i, scene in enumerate(scenes):
            lines.append(_scene_line(i, total, duration_seconds, scene, action_max))
        for key, label in (("sound", "SOUND"), ("call_to_action", "CLOSE"), ("avoid", "AVOID")):
            text = _sentence(getattr(parsed, key, ""))
            if text and key not in drop:
                lines.append(f"{label}: {text}")
        return "\n".join(lines)

    text = build()
    for drop in (("avoid",), ("avoid", "sound"), ("avoid", "sound", "look"), ("avoid", "sound", "look", "setting")):
        if len(text) <= VIDEO_MAX_CHARS:
            return text
        text = build(drop)
    for action_max in (140, 100, 70, 50):
        if len(text) <= VIDEO_MAX_CHARS:
            break
        text = build(("avoid", "sound", "look", "setting"), action_max)
    return text[:VIDEO_MAX_CHARS].rsplit("\n", 1)[0] if len(text) > VIDEO_MAX_CHARS else text


VIDEO_SYSTEM = VIDEO_SYSTEM.replace("{STYLE_MENU}", _style_menu())


def build_video_messages(idea: str, bc: dict, platform: str, duration_seconds: Optional[int],
                         today: datetime) -> list[dict]:
    facts = brand_facts(bc, styles=[])
    facts_text = "\n".join(f"- {k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in facts.items())
    total = scene_count(duration_seconds)
    lines = [f"Platform: {platform or 'not chosen yet'}",
             f"Target length: about {duration_seconds or 15} seconds",
             f"Scenes: EXACTLY {total} (one per {SECONDS_PER_SCENE} seconds)",
             f"Today: {today.strftime('%A %d %B %Y')}"]
    return [
        {"role": "system", "content": VIDEO_SYSTEM},
        {"role": "user", "content": f"BRAND FACTS\n{facts_text or '- (the Playbook is mostly empty)'}\n\n"
                                    f"VIDEO SETTINGS\n" + "\n".join(lines) +
                                    f"\n\nOWNER'S IDEA\n{idea.strip()}\n\nWrite the brief."},
    ]


async def expand_video(idea: str, bc: dict, platform: str = "", duration_seconds: Optional[int] = None) -> Optional[dict]:
    """The "Describe it" video flow's brief box: a short video idea in, a structured production brief out (see
    compose_video for the layout). {"expanded_prompt", "used", "prompt_version"} or None if the AI call failed."""
    today = datetime.now(timezone(timedelta(hours=1)))  # Lagos time
    req = AIService.build_ai_model(model=MODEL, temperature=0.6,
                                   messages=build_video_messages(idea, bc, platform, duration_seconds, today))
    try:
        completion = await AIService.structured_chat_completion(req, response_model=_ExpandedVideo)
        if isinstance(completion, dict) and "error" in completion:
            print(f"[PromptExpander] video expand failed: {completion['error']}")
            return None
        parsed: Optional[_ExpandedVideo] = completion.choices[0].message.parsed
    except Exception as e:
        print(f"[PromptExpander] video expand raised: {e}")
        return None
    if not parsed or not (parsed.concept.strip() or parsed.scenes):
        return None
    return {"expanded_prompt": compose_video(parsed, duration_seconds), "used": parsed.used[:6],
            "prompt_version": VIDEO_PROMPT_VERSION}
