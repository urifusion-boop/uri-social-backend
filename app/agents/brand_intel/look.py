"""
Picks a brand's look from what a read found: visual styles from the style library and a headline
and body font from the font library, the same choices setup and the Playbook offer.

For an Instagram or Facebook read Jane looks at the account's recent post images, so the choice
follows how its posts actually look; for a website she goes by its colours, logo and wording.
Every pick is a guess the owner checks ("Jane's guess"), and a failure here never fails the read.
"""
from __future__ import annotations

import json
from typing import Any

import openai

from app.agents.social_media_manager.services import style_library
from app.core.config import settings

from .extractor import MODELS

# The font library, as the frontend has it (src/data/fontLibrary.ts): the prompt text is what image
# generation reads, saved next to the slug exactly as setup saves it.
FONTS: dict[str, dict[str, str]] = {
    'serif_elegance': {"name": 'Serif Elegance', "description": 'Editorial, refined, high-end. Classic stroke contrast.',
        "prompt": 'All text in classic high-contrast serif typeface (Playfair Display, Didot, or Garamond style). Letterforms have elegant thin-to-thick stroke contrast. Editorial, refined, and premium feeling.'},
    'modern_sans': {"name": 'Modern Sans', "description": 'Clean, contemporary, minimal. The standard for modern brands.',
        "prompt": 'All text in clean geometric sans-serif typeface (Inter, Helvetica Neue, or Aktiv Grotesk style). Even stroke weight, precise letterforms, generous tracking. Contemporary and minimal.'},
    'bold_condensed': {"name": 'Bold Condensed', "description": 'Maximum impact. Loud, compressed, fills the frame.',
        "prompt": 'All text in massive bold condensed sans-serif (Bebas Neue, Impact, or Anton style). Zero tracking, characters packed tight, text fills maximum frame width. Commanding and high-energy.'},
    'script_flow': {"name": 'Script & Flow', "description": 'Flowing, elegant, personal. Cursive with calligraphic character.',
        "prompt": 'All text in flowing elegant script typeface (Great Vibes, Pinyon Script, or Pacifico style). Cursive, connected letterforms with natural calligraphic stroke variation. Personal and artisanal.'},
    'geometric_bold': {"name": 'Geometric Bold', "description": 'Structured, circular letterforms. Strong modern personality.',
        "prompt": 'All text in bold geometric sans-serif (Montserrat ExtraBold, Futura, or Circular style). Perfectly circular letterforms, strong visual weight, structured and commanding.'},
    'slab_serif': {"name": 'Slab Serif', "description": 'Sturdy, reliable, editorial weight. Bold with thick serifs.',
        "prompt": 'All text in bold slab serif typeface (Roboto Slab, Rockwell, or Clarendon style). Thick uniform serifs, sturdy and reliable letterforms. Editorial weight with approachable character.'},
    'display_expressive': {"name": 'Display Expressive', "description": 'Ultra-bold with extreme contrast. Text becomes a graphic element.',
        "prompt": 'All text in heavy display serif (Abril Fatface or Bodoni Poster style). Ultra-bold letterforms with extreme stroke contrast — hairline thins against massive thick strokes. Typography itself becomes a visual centrepiece.'},
    'minimal_light': {"name": 'Minimal Light', "description": 'Ultra-thin, airy, luxury through restraint. Whisper-thin letterforms.',
        "prompt": 'All text in ultra-light thin-weight serif (Cormorant, Bodoni 72, or Raleway Thin style). Whisper-thin letterforms with generous tracking and vast negative space. Luxury and sophistication conveyed through restraint.'},
    'street_block': {"name": 'Street Block', "description": 'Urban, industrial, stencil-cut. Military-inspired block letters.',
        "prompt": 'Typography style: bold block letters with a stencil-cut or military-industrial feel. Uppercase only. Letters have breaks or gaps as if cut from a stencil. Flat, heavy, no curves — all straight lines and sharp angles. The text looks spray-painted or stamped on concrete. Urban, raw, aggressive.'},
    'handwritten_casual': {"name": 'Handwritten Casual', "description": 'Personal, authentic, marker-pen feel. Like handwriting on a whiteboard.',
        "prompt": 'Typography style: casual handwritten text that looks like it was written with a thick marker pen on a whiteboard or paper. Slightly uneven baseline, imperfect letter sizing, natural pen-pressure variation. Not calligraphy — casual handwriting. Friendly, authentic, human. The text looks like a real person wrote it, not a font.'},
    'afro_display': {"name": 'Afro Display', "description": 'African-inspired, cultural, bold. Geometric patterns meet modern typography.',
        "prompt": 'Typography style: bold decorative display font with African-inspired geometric patterns and cultural motifs integrated into the letterforms. The letters have angular, tribal-influenced shapes with strong lines and cultural character. Modern Pan-African design — not stereotypical, but proudly African and contemporary. Bold, unapologetic, rooted in culture.'},
    'tech_mono': {"name": 'Tech Mono', "description": 'Code, data, digital precision. Every character occupies the same width.',
        "prompt": 'Typography style: monospace (fixed-width) font where every character occupies exactly the same horizontal space. Clean, precise, technical. The text looks like code on a terminal screen or data on a dashboard. Digital, mechanical, engineered. Often used with a code-editor-style dark background.'},
    'rounded_friendly': {"name": 'Rounded Friendly', "description": 'Approachable, warm, child-friendly. Rounded terminals and soft corners.',
        "prompt": 'Typography style: rounded sans-serif with soft, circular letter terminals and no sharp corners. Every endpoint of every stroke is rounded. The text feels warm, approachable, safe, and friendly. Medium weight. The text smiles at you. Non-threatening, inclusive, welcoming.'},
    'art_deco': {"name": 'Art Deco', "description": 'Gatsby-era luxury. Geometric, symmetric, vintage glamour.',
        "prompt": 'Typography style: Art Deco display font with strong geometric shapes, symmetric letterforms, and decorative inline details. Tall, narrow letterforms with geometric serifs and sometimes decorative lines within the strokes. Gatsby-era glamour. Jazz age. Old Hollywood. The text looks like it belongs on a luxury hotel entrance or a vintage champagne label.'},
    'brush_raw': {"name": 'Brush Raw', "description": 'Raw energy, painted, imperfect power. Thick brush strokes with visible texture.',
        "prompt": 'Typography style: raw brush script with thick, textured strokes that show the bristle marks of a real paint brush. Aggressive, energetic, imperfect. The text looks painted by hand with a large brush — not delicate calligraphy but bold, physical mark-making. Drips and splatter optional. The imperfection IS the beauty.'},
    'luxury_hairline': {"name": 'Luxury Hairline', "description": 'Whisper-thin, haute couture, Vogue. The thinnest possible serif.',
        "prompt": 'Typography style: ultra-thin serif with hairline strokes throughout — both the thick and thin strokes are extremely delicate. Wide letter-spacing. Uppercase for maximum effect. The text is barely there, like it was etched with a needle. Haute couture fashion energy. Vogue cover. The luxury of absence — the text says everything by almost not being there.'},
}

MAX_STYLES = 3
MAX_IMAGES = 6


def _prompt(fields: dict, site: dict, candidates: list[str]) -> str:
    def v(k):
        x = (fields.get(k) or {}).get("value")
        return ", ".join(map(str, x)) if isinstance(x, list) else (x or "")
    styles = "\n".join(f'- "{s}": {style_library.STYLES[s]["name"]} — {style_library.STYLES[s]["description"]}'
                       for s in candidates)
    fonts = "\n".join(f'- "{s}": {f["name"]} — {f["description"]}' for s, f in FONTS.items())
    return (
        "Choose the look for a small business's social media images.\n"
        + ("The images attached are its own recent posts: match how they look.\n" if site.get("images") else "")
        + f"Business: {v('brand_name')} — {v('industry')}. {v('product_description')}\n"
        f"Voice: {v('derived_voice')}. Colours: {v('brand_colors')}.\n\n"
        f"Visual styles (choose 1 to {MAX_STYLES}, best first):\n{styles}\n\n"
        f"Fonts (choose one for headlines and one for body text; they may be the same):\n{fonts}\n\n"
        'Return ONLY JSON: {"styles": ["<slug>", ...], "headline_font": "<slug>", "body_font": "<slug>", '
        '"why": "<one short sentence>"}'
    )


async def pick(fields: dict, site: dict) -> dict[str, dict[str, Any]]:
    """Playbook findings for the look: style_selections, primary_font, secondary_font (each a guess).
    Empty when there's nothing to go on or the model can't be reached."""
    if not settings.OPENAI_API_KEY:
        return {}
    industry = (fields.get("industry") or {}).get("value") or ""
    candidates = [s for s in style_library.get_styles_for_industry(str(industry)) if s in style_library.STYLES]
    if not candidates:
        return {}
    images = [u for u in (site.get("images") or []) if isinstance(u, str) and u.startswith("https://")][:MAX_IMAGES]
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    text = _prompt(fields, {**site, "images": images}, candidates)
    for with_images in ([True, False] if images else [False]):
        content: Any = text if not with_images else [{"type": "text", "text": text}] + [
            {"type": "image_url", "image_url": {"url": u, "detail": "low"}} for u in images]
        for model in MODELS:
            try:
                resp = await client.chat.completions.create(
                    model=model, response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": content}], timeout=60)
                return clean(json.loads(resp.choices[0].message.content or "{}"), candidates, site)
            except (openai.NotFoundError, openai.PermissionDeniedError):
                continue  # this account doesn't have the model: try the next one
            except openai.BadRequestError as e:
                if "model" in str(e).lower():
                    continue
                break  # most often an image the model couldn't fetch: try again without images
            except Exception as e:
                print(f"[brand_intel] look not picked: {e!r}", flush=True)
                return {}
    return {}


def clean(raw: dict, candidates: list[str], site: dict) -> dict[str, dict[str, Any]]:
    page = site.get("url", "")
    out: dict[str, dict[str, Any]] = {}
    styles = [s for s in dict.fromkeys(raw.get("styles") or []) if s in candidates][:MAX_STYLES]
    if styles:
        out["style_selections"] = {"value": styles, "basis": "inferred", "page": page}
    for key, field in (("headline_font", "primary_font"), ("body_font", "secondary_font")):
        if raw.get(key) in FONTS:
            out[field] = {"value": raw[key], "basis": "inferred", "page": page}
    return out


def companions(key: str, value: Any, profile: dict) -> dict[str, Any]:
    """What setup saves alongside these choices: each style's and font's prompt text, which is what
    image generation actually reads."""
    if key == "style_selections" and isinstance(value, list):
        old = dict(zip(profile.get("style_selections") or [], profile.get("style_prompt_fragments") or []))
        return {"style_prompt_fragments": [old.get(s) or style_library.get_prompt_fragment(s) for s in value]}
    if key in ("primary_font", "secondary_font") and value in FONTS:
        return {f"{key}_prompt": FONTS[value]["prompt"]}
    return {}
