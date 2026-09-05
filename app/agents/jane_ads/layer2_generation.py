"""
Layer 2 image generation for VSG-01 v3's generation-dependent formats
(§1.1, §1.8) — deliberately NOT creative.py's existing generate_ad_image()/
ImageContentService._generate_platform_image().

That pipeline is built for organic content, and by its own documentation
may render brand text/graphics directly into the pixels ("the engine may
render brand text into the graphic (that's how organic content looks)" —
generate_ad_image's own docstring). §1.1 is a hard rule that binds every
format in this library: "Layer 2 never produces readable text... every
format here is text-bearing... Garbled type in a paid ad reads as a scam
artefact in a market already screening for them (SEED-048). All copy,
prices, stars, badges and brand marks are placed in Layer 4." Reusing the
organic engine's own prompt construction would risk exactly the failure
this document exists to prevent.

What IS safely reusable is the raw primitive underneath that engine:
ImageContentService._call_dalle_api(prompt, size, ...) — a
prompt-to-image call with no brand-text-baking prompt construction wrapped
around it (that construction lives in the organic engine's callers, not
in this function). generate_scene() calls it directly with a prompt this
module builds itself, appending §1.8's global negative prompt verbatim.

_call_dalle_api returns a data: URL, not a hosted one —
DocumentRendererService's ai_generated_background layer fetches real
http(s) URLs only (confirmed: its _fetch_image has no data: URI support).
generate_scene uploads to Cloudinary first, mirroring the exact same
upload-then-use pattern generate_ad_image already established for the
same reason (Meta's ad-creative pipeline can't fetch a data: URI either).
"""
# §1.8 verbatim — appended to every Layer 2 generation call this module makes.
GLOBAL_NEGATIVE_PROMPT = (
    "no text, no lettering, no words, no numbers, no logos, no watermarks, no brand marks, "
    "no signage with readable text, no user interface elements, no buttons, no icons, "
    "no distorted hands, no extra fingers, no plastic skin, no waxy texture, "
    "no over-smoothed faces, no HDR halo, no lens flare, no stock-photo styling, "
    "no Western suburban setting, no snow, no autumn foliage"
)


class SceneGenerationFailed(RuntimeError):
    """Raised when the underlying generation call or the follow-up
    Cloudinary upload fails — a format's render() should not silently
    proceed with a missing background."""
    pass


async def generate_scene(prompt: str, size: str = "1080x1080") -> str:
    """
    Generate a single Layer 2 scene image and return a real hosted URL.

    prompt: the scene description ONLY — this function appends
    GLOBAL_NEGATIVE_PROMPT itself, so callers should not duplicate it.
    size: "WIDTHxHEIGHT" — passed straight through to _call_dalle_api,
    which internally buckets to the nearest square/landscape/portrait
    generation size and crops to the exact requested dimensions.

    Raises SceneGenerationFailed on any failure rather than returning None
    — every caller in this format library needs a real background to
    composite text onto; there is no meaningful "partial" result.
    """
    from app.agents.social_media_manager.services.image_content_service import ImageContentService

    full_prompt = f"{prompt.strip()} {GLOBAL_NEGATIVE_PROMPT}"
    result = await ImageContentService._call_dalle_api(full_prompt, size=size)
    if not result.get("success"):
        raise SceneGenerationFailed(f"Layer 2 generation failed: {result.get('error')}")

    image_url = result["url"]
    if image_url.startswith("data:"):
        from app.utils.cloudinary_upload import upload_base64
        try:
            image_url = await upload_base64(image_url, folder="uri-social/jane-ads/vsg01")
        except Exception as e:
            raise SceneGenerationFailed(f"Cloudinary upload failed: {e}") from e

    return image_url
