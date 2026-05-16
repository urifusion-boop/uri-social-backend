# app/agents/social_media_manager/services/custom_font_service.py

"""
Custom Font Service
Handles custom font upload, analysis, and management for Typography System.

Features:
- Upload .ttf/.otf font files to Cloudinary
- Render font samples for analysis
- Analyze fonts using GPT-4o-mini Vision API
- Generate prompt directives for AI image generation
"""

from typing import Dict, Any, Optional
import io
import base64
import httpx
from PIL import Image, ImageDraw, ImageFont
from app.services.AIService import client as openai_client


class CustomFontService:
    """Service for managing custom font uploads and analysis"""

    @staticmethod
    async def analyze_font(font_url: str) -> Dict[str, Any]:
        """
        Analyze a custom font using GPT-4o-mini Vision API.

        Process:
        1. Download the font file
        2. Render sample text in the font
        3. Send sample to GPT-4o-mini for analysis
        4. Extract prompt directive for image generation

        Args:
            font_url: Cloudinary URL of the uploaded font file

        Returns:
            {
                "analysis": {
                    "font_category": "sans-serif",
                    "stroke_weight": "bold",
                    "letter_shape": "geometric",
                    "terminals": "rounded",
                    "overall_feel": "Modern, clean, corporate"
                },
                "prompt_directive": "Typography style: clean geometric sans-serif..."
            }
        """
        try:
            # Step 1: Download the font file
            print(f"[CUSTOM_FONT] Downloading font from: {font_url}")
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(font_url)
                if response.status_code != 200:
                    raise Exception(f"Failed to download font: {response.status_code}")
                font_bytes = response.content

            # Step 2: Render sample text using the font
            print(f"[CUSTOM_FONT] Rendering font sample...")
            sample_image_b64 = await CustomFontService._render_font_sample(font_bytes)

            # Step 3: Analyze with GPT-4o-mini Vision
            print(f"[CUSTOM_FONT] Analyzing font with GPT-4o-mini Vision...")
            analysis = await CustomFontService._analyze_with_vision(sample_image_b64)

            print(f"[CUSTOM_FONT] ✅ Analysis complete!")
            return analysis

        except Exception as e:
            print(f"[CUSTOM_FONT] ❌ Error analyzing font: {e}")
            raise Exception(f"Font analysis failed: {str(e)}")

    @staticmethod
    async def _render_font_sample(font_bytes: bytes) -> str:
        """
        Render sample text using the custom font.

        Creates an image showing:
        - Alphabet in multiple cases
        - Numbers
        - Various sizes to show the font's characteristics

        Returns:
            Base64-encoded PNG image
        """
        try:
            # Save font temporarily to load with PIL
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix='.ttf') as tmp:
                tmp.write(font_bytes)
                tmp_path = tmp.name

            # Create image
            img_width = 1000
            img_height = 600
            img = Image.new('RGB', (img_width, img_height), color='white')
            draw = ImageDraw.Draw(img)

            try:
                # Load font at different sizes
                font_large = ImageFont.truetype(tmp_path, 72)
                font_medium = ImageFont.truetype(tmp_path, 48)
                font_small = ImageFont.truetype(tmp_path, 32)

                # Draw sample text
                y_offset = 50

                # Large headline text
                draw.text((50, y_offset), "Aa Bb Cc Dd", fill='black', font=font_large)
                y_offset += 100

                # Medium alphabet
                draw.text((50, y_offset), "ABCDEFGHIJKLM", fill='black', font=font_medium)
                y_offset += 70
                draw.text((50, y_offset), "NOPQRSTUVWXYZ", fill='black', font=font_medium)
                y_offset += 80

                # Small lowercase + numbers
                draw.text((50, y_offset), "abcdefghijklmnopqrstuvwxyz", fill='black', font=font_small)
                y_offset += 50
                draw.text((50, y_offset), "0123456789 !@#$%^&*()", fill='black', font=font_small)

            finally:
                # Clean up temp file
                import os
                os.unlink(tmp_path)

            # Convert to base64
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            img_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

            return f"data:image/png;base64,{img_b64}"

        except Exception as e:
            print(f"[CUSTOM_FONT] Error rendering font sample: {e}")
            raise

    @staticmethod
    async def _analyze_with_vision(sample_image_b64: str) -> Dict[str, Any]:
        """
        Use GPT-4o-mini Vision API to analyze the font sample.

        The AI analyzes visual characteristics and generates a prompt directive
        that GPT-Image-2 can use to approximate this font style.
        """
        try:
            prompt = """Analyze this font sample in detail. Your analysis will be used to instruct an AI image generator (GPT-Image-2) to render text in a similar style.

Return a JSON object with this exact structure:

{
  "font_category": "serif | sans-serif | slab-serif | script | display | monospace | handwritten | decorative",
  "stroke_weight": "ultra-thin | thin | light | regular | medium | bold | extra-bold | ultra-bold",
  "stroke_contrast": "none | low | medium | high | extreme",
  "letter_shape": "geometric | humanist | grotesque | rounded | angular | organic | structured",
  "terminals": "sharp | rounded | flat | tapered | ball",
  "x_height": "small | medium | large",
  "letter_spacing": "tight | normal | wide | very-wide",
  "special_features": ["list any distinctive characteristics like serifs, swashes, decorative elements, etc."],
  "overall_feel": "2-3 word description of the font personality (e.g., 'Modern, clean, corporate' or 'Elegant, refined, luxury')",
  "prompt_directive": "A 50-80 word detailed description of this font that an image generation AI can use to render matching text. Start with 'Typography style:' and describe stroke weight, letter shapes, terminals, spacing, contrast, and overall character. Be specific and visual."
}

Focus on creating a prompt_directive that is descriptive enough for an AI to approximate this font style visually."""

            import asyncio
            loop = asyncio.get_running_loop()

            response = await loop.run_in_executor(
                None,
                lambda: openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": sample_image_b64}}
                        ]
                    }],
                    max_tokens=500,
                    temperature=0,
                    response_format={"type": "json_object"}
                )
            )

            # Parse JSON response
            import json
            analysis_json = response.choices[0].message.content
            analysis = json.loads(analysis_json)

            # Extract the prompt directive
            prompt_directive = analysis.get("prompt_directive", "")

            # Remove the prompt_directive from analysis (we'll store it separately)
            analysis_data = {k: v for k, v in analysis.items() if k != "prompt_directive"}

            return {
                "analysis": analysis_data,
                "prompt_directive": prompt_directive
            }

        except Exception as e:
            print(f"[CUSTOM_FONT] Error in Vision API analysis: {e}")
            raise
