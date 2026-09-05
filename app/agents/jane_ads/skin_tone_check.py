"""
Skin-tone rendering verification — VSG-01 v3 §1.7.

"Verify skin rendering on every generation. Models lighten under bright-
light prompts. This requires an automated check — the failure is
systematic, not occasional."

Every Layer 2 prompt is required (§1.7, structurally enforced for the
customer-description slot by visual_slots.build_customer_description) to
specify "deep brown to dark brown skin tones, West African features" — but
specifying it in the prompt doesn't guarantee the model honoured it. This
module is the check on the OUTPUT, not the input: a real vision-model call
against a generated image, following the same pattern
ProductAnalysisService already uses for product-fidelity QA (GPT-4o-mini
vision, JSON-only response, temperature=0, ~$0.003/call).

Unlike legibility.py's compression test, this genuinely cannot be
determined from document metadata alone — it's a property of pixels a
generation model produced, not something derivable from layout JSON.

Verified live against the real dev credential (AWS SSM
/uri/social-backend/dev/OPENAI_API_KEY — not the stale key in this repo's
local .env, which is unrelated to what the deployed dev backend actually
uses and doesn't work; this module's own local dev-environment test
initially reached the wrong conclusion for exactly that reason before the
real SSM-sourced key was checked) against three real stock photos: a
person-less object photo (contains_person=False, correctly a vacuous
pass), a lighter-skinned portrait (assessed "medium brown," correctly
matches_target_range=False), and a dark-skinned portrait (assessed "deep
brown," correctly matches_target_range=True).

Not yet wired into a live call path: no L2 (generation-dependent) format
exists in this codebase yet (VSG-01 step 7). This is the primitive that
step calls on every generated image before it reaches Layer 4.
"""
import json
from typing import Any, Dict

_VERIFICATION_PROMPT = """Assess the skin tone of any person visible in this image, for a Nigerian audience representation check.

Return JSON only (no markdown, no code blocks, just raw JSON):
{
  "contains_person": true or false,
  "skin_tone_observed": "one short phrase describing the skin tone actually rendered, e.g. 'deep brown', 'medium brown', 'light olive', 'fair/light' - or null if no person is visible",
  "matches_target_range": true or false,
  "confidence": "high" or "medium" or "low",
  "notes": "one short sentence explaining the assessment"
}

The target range is "deep brown to dark brown" (West African skin tones). Set matches_target_range to false if the person rendered is fair, light, or medium-light - even if a prompt requested the target range; models are known to lighten skin under bright-light prompts, and that is exactly the failure this check exists to catch. If no person is visible in the image, set contains_person to false and matches_target_range to true (there is nothing to fail).

CRITICAL RULES:
1. ONLY describe what you can actually see in the image.
2. Do NOT assume compliance because a prompt requested it - judge the pixels only.
3. Return ONLY the JSON, no explanations, no markdown formatting."""


class SkinToneMismatch(ValueError):
    """§1.7's automated block (§5 item 3): a person is visible in the
    generated image and their rendered skin tone falls outside the deep-
    brown-to-dark-brown target range — the systematic 'models lighten
    under bright-light prompts' failure this check exists to catch."""
    pass


async def verify_skin_rendering(image_url: str) -> Dict[str, Any]:
    """Real vision-model call against a generated image. Never raises on
    its own — returns a fail-safe (non-compliant) result on any API/parse
    error so a transient outage doesn't silently pass through as
    compliant. assert_skin_rendering_compliant is the actual gate."""
    from app.services.AIService import AIService

    try:
        ai_request = AIService.build_ai_model(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": _VERIFICATION_PROMPT},
                ],
            }],
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=200,
        )
        ai_response = await AIService.chat_completion(ai_request)
        if isinstance(ai_response, dict) and "error" in ai_response:
            raise Exception(ai_response["error"])

        raw = ai_response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.split("\n") if not line.startswith("```"))
        result = json.loads(raw)

        # A known GPT-4o-mini vision quirk, previously diagnosed in this
        # exact codebase (ProductAnalysisService's own preservation-block
        # bug): it sometimes returns the literal string "null" instead of
        # a real JSON null for an optional field the prompt describes as
        # nullable — this prompt says the same thing about
        # skin_tone_observed ("or null if no person is visible").
        skin_tone_observed = result.get("skin_tone_observed")
        if isinstance(skin_tone_observed, str) and skin_tone_observed.strip().lower() == "null":
            skin_tone_observed = None

        # Normalise defensively — a vision model's JSON output is not a
        # typed contract, unlike everything else this format library reads.
        return {
            "contains_person": bool(result.get("contains_person", False)),
            "skin_tone_observed": skin_tone_observed,
            "matches_target_range": bool(result.get("matches_target_range", True)),
            "confidence": result.get("confidence", "low"),
            "notes": result.get("notes", ""),
        }
    except Exception as e:
        print(f"⚠️ Skin-tone verification error: {e}", flush=True)
        return {
            "contains_person": True,
            "skin_tone_observed": None,
            "matches_target_range": False,
            "confidence": "low",
            "notes": f"verification call failed: {e}",
        }


async def assert_skin_rendering_compliant(image_url: str) -> Dict[str, Any]:
    """The automated block itself (§5 item 3: "Skin tones accurate" is one
    of the pre-flight items automated blocks, not review prompts, cover).
    Raises SkinToneMismatch if a person is visible and their rendered skin
    tone doesn't match the target range — including when verification
    itself failed (see verify_skin_rendering's fail-safe result)."""
    result = await verify_skin_rendering(image_url)
    if result["contains_person"] and not result["matches_target_range"]:
        raise SkinToneMismatch(
            f"skin tone observed ({result['skin_tone_observed']!r}) does not match "
            f"the deep-brown-to-dark-brown target range — {result['notes']}"
        )
    return result
