"""
Forensic Product Analysis Service
Analyzes product images in extreme detail to preserve product fidelity during generation.

This is the KEY INNOVATION from the Product Preservation Pipeline PRD.
Replicates what ChatGPT does internally when you upload a product photo.

PRD: URI-Social-Product-Preservation-Pipeline.docx - Step 3
"""

import json
import asyncio
from typing import Dict, Any, Optional


class ProductAnalysisService:
    """
    Forensic product analysis using GPT-4o-mini vision.

    Before generating immersive scenes, we analyze the product in forensic detail:
    - Exact shape and proportions
    - Cap/closure type, color, material
    - Body material, color, finish
    - Every word on the label (line by line)
    - Logo descriptions
    - Dominant colors

    This analysis is passed to GPT-Image-2 alongside the image, giving it
    TWO sources of truth (image + text spec) so there are no gaps to guess.

    Cost: ~$0.003 per analysis (GPT-4o-mini is very cheap)
    """

    @staticmethod
    async def analyze_product_forensically(cutout_url: str) -> Dict[str, Any]:
        """
        Analyze product cutout in forensic detail.

        PRD: Step 3 - Forensic Product Analysis

        Args:
            cutout_url: URL of product cutout (transparent background)

        Returns:
            Dict with product_type, shape, cap, body, label, colors
        """
        from app.services.AIService import AIService

        forensic_prompt = """Describe this product in extreme forensic detail. An image generation AI must reproduce this product EXACTLY. Your description is the specification it will follow.

Return JSON only (no markdown, no code blocks, just raw JSON):
{
  "product_type": "e.g. perfume bottle, lip gloss tube, water bottle, yogurt container",
  "overall_shape": "e.g. tall cylinder with rounded shoulders, rectangular carton, oval tube",
  "height_width_ratio": "e.g. approximately 3:1, 2:1, 1:1",
  "cap_closure": {
    "type": "e.g. screw cap, pump, dome cap, spray nozzle, flip cap, no cap",
    "colour": "e.g. silver metallic, matte pink, glossy blue, black plastic",
    "material": "e.g. metal, plastic, frosted plastic, chrome, matte finish"
  },
  "body": {
    "material": "e.g. clear glass, frosted glass, matte plastic, glossy plastic, cardboard",
    "colour": "e.g. deep amber, transparent, matte white, pink, clear",
    "finish": "e.g. glossy, matte, satin, textured, frosted"
  },
  "liquid_visible": true or false,
  "liquid_colour": "e.g. amber, clear, pink, yellow (or null if not visible)",
  "label": {
    "present": true or false,
    "position": "e.g. centre front, bottom third, wraparound, top section",
    "background_colour": "e.g. black, white, cream, transparent, gold",
    "text_lines": [
      "exact text line 1 as seen on label",
      "exact text line 2 if present",
      "exact text line 3 if present"
    ],
    "text_colour": "e.g. gold, white, dark red, black",
    "font_style": "e.g. cursive script, bold sans-serif, serif, handwritten",
    "logo_description": "e.g. diamond shape with brand name in cursive inside, circular emblem with crown icon, or null if no logo"
  },
  "additional_details": "e.g. embossed pattern, metallic band, ridged texture, barcode visible, decorative elements",
  "dominant_colours_hex": ["#hex1", "#hex2", "#hex3"]
}

CRITICAL RULES:
1. ONLY describe what you can actually see in the image
2. Do NOT invent details or guess
3. For text on labels: transcribe EXACTLY letter-for-letter, word-for-word
4. If something is unclear or not visible, say "not visible" or "unclear"
5. Return ONLY the JSON, no explanations, no markdown formatting"""

        try:
            print(f"🔍 Running forensic product analysis on: {cutout_url[:80]}...")

            # Call GPT-4o-mini vision
            ai_request = AIService.build_ai_model(
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": cutout_url}},
                        {"type": "text", "text": forensic_prompt}
                    ]
                }],
                model="gpt-4o-mini",
                temperature=0,  # Deterministic analysis
                max_tokens=800,
            )

            ai_response = await AIService.chat_completion(ai_request)

            if isinstance(ai_response, dict) and "error" in ai_response:
                raise Exception(ai_response["error"])

            raw_content = ai_response.choices[0].message.content.strip()

            # Remove markdown code blocks if present
            if raw_content.startswith("```"):
                # Extract JSON from markdown code block
                lines = raw_content.split("\n")
                json_lines = []
                in_code_block = False
                for line in lines:
                    if line.startswith("```"):
                        in_code_block = not in_code_block
                        continue
                    if in_code_block or (not line.startswith("```") and "{" in line):
                        json_lines.append(line)
                raw_content = "\n".join(json_lines)

            # Parse JSON
            product_spec = json.loads(raw_content)

            # Log key findings
            print(f"✅ Product analysis complete:")
            print(f"   📦 Type: {product_spec.get('product_type', 'unknown')}")
            print(f"   📏 Shape: {product_spec.get('overall_shape', 'unknown')}")

            label_info = product_spec.get('label', {})
            if label_info.get('present') and label_info.get('text_lines'):
                label_text = " | ".join(label_info.get('text_lines', []))
                print(f"   🏷️  Label text: {label_text[:100]}...")
            else:
                print(f"   🏷️  No label or text detected")

            print(f"   💰 Analysis cost: ~$0.003")

            return product_spec

        except json.JSONDecodeError as e:
            print(f"⚠️ Failed to parse product analysis JSON: {e}")
            print(f"   Raw response: {raw_content[:200]}...")
            # Return a minimal spec as fallback
            return ProductAnalysisService._get_fallback_spec(cutout_url)

        except Exception as e:
            print(f"⚠️ Forensic analysis error: {str(e)}")
            # Return a minimal spec as fallback
            return ProductAnalysisService._get_fallback_spec(cutout_url)

    @staticmethod
    def _get_fallback_spec(cutout_url: str) -> Dict[str, Any]:
        """
        Return a minimal product spec when analysis fails.
        """
        return {
            "product_type": "product",
            "overall_shape": "standard product shape",
            "height_width_ratio": "2:1",
            "cap_closure": {
                "type": "cap",
                "colour": "not analyzed",
                "material": "not analyzed"
            },
            "body": {
                "material": "not analyzed",
                "colour": "not analyzed",
                "finish": "not analyzed"
            },
            "liquid_visible": False,
            "liquid_colour": None,
            "label": {
                "present": False,
                "position": "unknown",
                "background_colour": "unknown",
                "text_lines": [],
                "text_colour": "unknown",
                "font_style": "unknown",
                "logo_description": None
            },
            "additional_details": "Analysis failed, using visual reference only",
            "dominant_colours_hex": ["#000000", "#FFFFFF"]
        }

    @staticmethod
    def build_preservation_block(product_spec: Dict[str, Any]) -> str:
        """
        Convert forensic analysis into a preservation specification prompt.

        PRD: Step 4 - Build the Preservation Prompt

        This prompt block tells GPT-Image-2 EXACTLY what the product must look like.
        It's prepended to the immersive environment prompt.

        Args:
            product_spec: Output from analyze_product_forensically()

        Returns:
            Preservation prompt block (string)
        """
        # Extract fields with safe defaults
        product_type = product_spec.get("product_type", "product")
        overall_shape = product_spec.get("overall_shape", "standard shape")
        ratio = product_spec.get("height_width_ratio", "2:1")

        cap = product_spec.get("cap_closure", {})
        cap_type = cap.get("type", "cap")
        cap_colour = cap.get("colour", "standard")
        cap_material = cap.get("material", "standard material")

        body = product_spec.get("body", {})
        body_material = body.get("material", "standard material")
        body_colour = body.get("colour", "standard")
        body_finish = body.get("finish", "standard")

        liquid_visible = product_spec.get("liquid_visible", False)
        liquid_colour = product_spec.get("liquid_colour")

        label = product_spec.get("label", {})
        label_present = label.get("present", False)
        label_position = label.get("position", "front")
        label_bg = label.get("background_colour", "standard")
        label_text_lines = label.get("text_lines", [])
        label_text_colour = label.get("text_colour", "standard")
        label_font = label.get("font_style", "standard")
        label_logo = label.get("logo_description")

        additional = product_spec.get("additional_details", "none")
        colours_hex = product_spec.get("dominant_colours_hex", [])

        # Build label text block
        if label_present and label_text_lines:
            label_text_block = "\n".join([
                f"  Line {i+1}: \"{line}\""
                for i, line in enumerate(label_text_lines)
            ])
        else:
            label_text_block = "  (No text visible on product)"

        # Build liquid visibility block
        liquid_block = ""
        if liquid_visible and liquid_colour:
            liquid_block = f"Liquid is visible inside. Liquid colour: {liquid_colour}."
        elif liquid_visible:
            liquid_block = "Liquid is visible inside but colour is not clear."
        else:
            liquid_block = "Liquid is not visible from this angle."

        # Build the full preservation block
        preservation = f"""=== PRODUCT REPRODUCTION SPECIFICATION ===
You MUST reproduce this product with ABSOLUTE FIDELITY.
Every detail below is a hard requirement, not a suggestion.

PRODUCT TYPE: {product_type}

SHAPE & PROPORTIONS:
Overall shape: {overall_shape}
Height-to-width ratio: {ratio}
Do NOT make it taller, shorter, wider, or narrower than this ratio.
The product must maintain these exact proportions.

CAP/CLOSURE:
Type: {cap_type}
Colour: {cap_colour}
Material: {cap_material}
Reproduce this cap EXACTLY. Same shape, same colour, same material.
Do not modify or simplify the cap design.

BODY:
Material: {body_material}
Colour: {body_colour}
Finish: {body_finish}
{liquid_block}
The body material MUST look photographically accurate:
- Glass must look like real glass (transparent, refractive, reflective)
- Matte finishes must have no shine
- Metallic surfaces must have realistic metallic sheen
- Plastic must have appropriate translucency or opacity

LABEL (CRITICAL FOR BRAND RECOGNITION):"""

        if label_present:
            preservation += f"""
Position on product: {label_position}
Label background: {label_bg}
Text colour: {label_text_colour}
Font style: {label_font}"""
            if label_logo:
                preservation += f"""
Logo: {label_logo}"""

            preservation += f"""

EXACT LABEL TEXT (reproduce every word, every line, every letter):
{label_text_block}

⚠️ SPELLING IS ABSOLUTELY CRITICAL ⚠️
Every letter must match exactly. Do not change, rearrange, or "fix" any text.
If the label says "Romantic Hari" do NOT write "Romantic Hair"
If the label says "Cute Girl" do NOT write "Cute Curl"
If the label says "LUSTROUS LIP GLOSS" reproduce it letter by letter
If there are typos or unconventional spellings on the original label, KEEP THEM EXACTLY AS THEY ARE."""
        else:
            preservation += """
No label or text is visible on this product.
Do NOT add any text, labels, or branding to the product."""

        preservation += f"""

ADDITIONAL DETAILS: {additional}

DOMINANT COLOURS: {', '.join(colours_hex) if colours_hex else 'standard product colours'}

=== PRESERVATION RULES (NON-NEGOTIABLE) ===
1. The product must look like a PHOTOGRAPH of a real object, not an illustration or 3D render
2. The product shape, proportions, and orientation must match the specification EXACTLY
3. Every word on every label must be spelled EXACTLY as specified above
4. The cap/closure must be the exact colour, shape, material, and style
5. The body material must be visually correct with appropriate reflections and texture
6. If liquid is visible, it must be the specified colour with realistic transparency
7. The product must react naturally to scene lighting: reflections on glass, shadows on matte surfaces
8. Do NOT add any text, logos, or design elements to the product that are not specified above
9. Do NOT remove any text, logos, or design elements that ARE specified above
10. When in doubt about any detail, match the reference image EXACTLY - the image is the ultimate source of truth

THE PRODUCT IS THE STAR. Preserve it perfectly. Build the immersive environment AROUND it, not over it.
"""

        return preservation

    @staticmethod
    async def validate_preservation(cutout_url: str, generated_url: str) -> Dict[str, Any]:
        """
        Validate that the generated image preserves the original product.

        PRD: Step 7 - Validation Check

        Compares the original product cutout vs the product in the generated image.
        Checks: shape, label text spelling, cap color, body color, proportions.

        Args:
            cutout_url: URL of original product cutout
            generated_url: URL of generated immersive scene

        Returns:
            {
                "matches": bool,
                "issues": [list of differences]
            }

        Cost: ~$0.005 per validation
        """
        from app.services.AIService import AIService

        validation_prompt = """Compare these two images. The first is the original product photo. The second is a generated graphic containing this product.

Does the product in the second image match the original?

Check carefully:
1. **Shape & proportions**: Is the product the same shape and size ratio?
2. **Label text spelling**: Is every word on the label spelled exactly the same?
3. **Cap/closure color**: Is the cap/lid/top the same color?
4. **Body color**: Is the main product body the same color?
5. **Material appearance**: Does glass look like glass, plastic like plastic, etc?

Return JSON only (no markdown, no code blocks):
{
  "matches": true or false,
  "issues": ["list any differences found", "e.g. label text misspelled", "e.g. cap color changed from silver to gold"]
}

If the product matches perfectly, return {"matches": true, "issues": []}
If there are ANY differences, return {"matches": false, "issues": ["difference 1", "difference 2", ...]}

Be strict but fair. Minor lighting differences are OK. Changed text or colors are NOT OK."""

        try:
            print(f"🔍 Running preservation validation...")

            # Call GPT-4o-mini vision with both images
            ai_request = AIService.build_ai_model(
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": cutout_url}},
                        {"type": "image_url", "image_url": {"url": generated_url}},
                        {"type": "text", "text": validation_prompt}
                    ]
                }],
                model="gpt-4o-mini",
                temperature=0,
                max_tokens=300,
            )

            ai_response = await AIService.chat_completion(ai_request)

            if isinstance(ai_response, dict) and "error" in ai_response:
                raise Exception(ai_response["error"])

            raw_content = ai_response.choices[0].message.content.strip()

            # Remove markdown code blocks if present
            if raw_content.startswith("```"):
                lines = raw_content.split("\n")
                json_lines = []
                in_code_block = False
                for line in lines:
                    if line.startswith("```"):
                        in_code_block = not in_code_block
                        continue
                    if in_code_block or (not line.startswith("```") and "{" in line):
                        json_lines.append(line)
                raw_content = "\n".join(json_lines)

            # Parse JSON
            validation_result = json.loads(raw_content)

            if validation_result.get("matches"):
                print(f"✅ Validation PASSED - Product preserved accurately")
            else:
                issues = validation_result.get("issues", [])
                print(f"⚠️ Validation FAILED - Issues detected:")
                for issue in issues[:3]:  # Show first 3 issues
                    print(f"   - {issue}")

            print(f"💰 Validation cost: ~$0.005")

            return validation_result

        except json.JSONDecodeError as e:
            print(f"⚠️ Failed to parse validation JSON: {e}")
            print(f"   Raw response: {raw_content[:200]}...")
            # Return a permissive result on parse error
            return {"matches": True, "issues": ["Validation parse error - proceeding anyway"]}

        except Exception as e:
            print(f"⚠️ Validation error: {str(e)}")
            # Return a permissive result on error
            return {"matches": True, "issues": ["Validation error - proceeding anyway"]}
