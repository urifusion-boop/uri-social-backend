# app/agents/social_media_manager/services/image_editing_service.py

"""
Image Editing Service
In-Place Image Editing PRD Implementation

Handles:
- Edit Intent Classification (GPT-4o-mini)
- Edit Prompt Building (PRESERVE_BLOCK + category templates)
- Image editing via GPT-Image-2 edit API
- Version history management
- Credit integration
- Undo functionality
"""

from typing import Dict, Any, Optional
from datetime import datetime
import uuid
import io
import base64
import httpx

from app.domain.responses.uri_response import UriResponse
from app.services.AIService import AIService, client as openai_client
from app.services.CreditService import credit_service


class ImageEditingService:
    """
    In-place image editing service
    PRD: URI-Social-Image-Editing-PRD.docx
    """

    # ========== EDIT CATEGORIES ==========
    # PRD Section 2: Four edit categories with different costs
    EDIT_CATEGORIES = {
        "text_edit": {"cost": 0, "api_cost": 0.05},      # FREE
        "style_edit": {"cost": 0, "api_cost": 0.05},     # FREE
        "content_edit": {"cost": 1, "api_cost": 0.10},   # 1 free, then 1 credit
        "full_redesign": {"cost": 1, "api_cost": 0.15},  # 1 credit
    }

    # ========== UNDO TRIGGERS ==========
    # PRD Section 6.2: WhatsApp undo detection
    UNDO_TRIGGERS = [
        'go back', 'previous version', 'undo', 'revert',
        'the old one', 'before the change', 'the one before',
        'bring back', 'restore', 'last version', 'original'
    ]

    # ========== PRESERVE BLOCK ==========
    # PRD Section 4.1: Anchors the model to preserve existing elements
    PRESERVE_BLOCK = """
CRITICAL INSTRUCTION: You are EDITING an existing image, not creating
a new one. You must preserve the following elements EXACTLY as they
appear in the original image:

- All text content, fonts, colours, sizes, and positions
  (UNLESS the edit specifically targets text)
- All product photography and main subject matter
  (UNLESS the edit specifically targets the subject)
- Overall composition, layout, and element arrangement
- Call-to-action text and its placement
- Brand colour usage throughout the design
- Background treatment (UNLESS the edit specifically targets background)
- All decorative elements and design details
- Margins and spacing between elements

Change ONLY what is explicitly requested below.

Every other pixel must remain identical to the original image.
If you are uncertain whether something should change, DO NOT change it.
"""

    @staticmethod
    async def classify_edit_intent(user_feedback: str) -> str:
        """
        Classify user's edit request into one of 4 categories
        PRD Section 3.1: Edit Intent Classifier

        Args:
            user_feedback: User's edit request (e.g., "Change the price to ₦4,500")

        Returns:
            Category: 'text_edit', 'style_edit', 'content_edit', or 'full_redesign'
        """
        try:
            from app.domain.requests.ai_request import AIRequest

            ai_request = AIRequest(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """Classify this image edit request into exactly one category.
Respond with ONLY the category name, nothing else.

Categories:
- text_edit: Changing, fixing, updating, or correcting any text
  content in the image (words, numbers, prices, dates, names,
  typos, phone numbers, URLs)
- style_edit: Changing visual properties without changing content
  (colours, brightness, contrast, font size, element position,
  background shade, spacing, margins, text colour)
- content_edit: Adding new elements, removing elements, swapping
  images, replacing backgrounds with new scenes, adding icons or
  decorative elements
- full_redesign: The user wants a completely different image,
  new concept, new layout, start from scratch, or expresses total
  dissatisfaction with the current image"""
                    },
                    {"role": "user", "content": user_feedback}
                ],
                max_tokens=10,
                temperature=0,
            )

            response = await AIService.chat_completion(ai_request)
            category = response.choices[0].message.content.strip().lower()

            # Validate response
            valid_categories = ['text_edit', 'style_edit', 'content_edit', 'full_redesign']
            if category not in valid_categories:
                print(f"[EDIT] Classifier returned invalid category: {category}")
                return 'content_edit'  # Safe default

            print(f"[EDIT] Classified as: {category} | Feedback: {user_feedback}")
            return category

        except Exception as e:
            print(f"[EDIT] Classifier error: {e}")
            return 'content_edit'  # Safe default

    @staticmethod
    def is_undo_request(message: str) -> bool:
        """
        Detect if user wants to undo last edit
        PRD Section 6.2: WhatsApp undo detection
        """
        lower = message.lower()
        return any(trigger in lower for trigger in ImageEditingService.UNDO_TRIGGERS)

    @staticmethod
    def build_text_edit_prompt(user_feedback: str) -> str:
        """
        Build prompt for text-only edits
        PRD Section 4.2: Text Edit Prompt Template
        """
        return f"""{ImageEditingService.PRESERVE_BLOCK}

EDIT TYPE: Text modification only.

REQUESTED CHANGE: {user_feedback}

RULES FOR THIS EDIT:
- Change ONLY the specific text mentioned in the request
- Keep the EXACT same font family, weight, size, and colour
  for the changed text
- Keep the text in the EXACT same position and alignment
- Do not move, resize, or restyle any other text elements
- Do not change the background, product image, or any visual element
- The result should be indistinguishable from the original except
  for the specific text that was changed"""

    @staticmethod
    def build_style_edit_prompt(user_feedback: str) -> str:
        """
        Build prompt for style/visual edits
        PRD Section 4.2: Style Edit Prompt Template
        """
        return f"""{ImageEditingService.PRESERVE_BLOCK}

EDIT TYPE: Visual style modification only.

REQUESTED CHANGE: {user_feedback}

RULES FOR THIS EDIT:
- Modify ONLY the specific visual property mentioned (colour, size,
  position, brightness, contrast, etc.)
- Do not change any text content — all words, numbers, and text
  must remain exactly the same
- Do not change the product image or main subject
- Do not alter the overall composition or layout structure
- The change should feel like adjusting a setting, not redesigning"""

    @staticmethod
    def build_content_edit_prompt(user_feedback: str) -> str:
        """
        Build prompt for content edits (add/remove elements)
        PRD Section 4.2: Content Edit Prompt Template
        """
        return f"""{ImageEditingService.PRESERVE_BLOCK}

EDIT TYPE: Content modification.

REQUESTED CHANGE: {user_feedback}

RULES FOR THIS EDIT:
- Add, remove, or modify ONLY the specific element mentioned
- When adding an element, match the existing visual style,
  colour palette, and design language of the original image
- When removing an element, fill the space naturally with the
  surrounding background treatment
- Maintain all existing text, CTA, and brand elements unless
  they conflict with the requested change
- The modified image should look like it was designed this way
  from the start, not like something was awkwardly patched in"""

    @staticmethod
    def get_edit_confirmation_message(edit_category: str, user_feedback: str) -> str:
        """
        Generate Jane's response message after successful edit
        PRD Section 8: Jane's Response Messages

        Creates personalized messages that confirm what changed and reassure
        that everything else was preserved.
        """
        feedback_lower = user_feedback.lower()

        if edit_category == "text_edit":
            # Try to extract what text changed
            if "price" in feedback_lower:
                return "Updated! Only the price changed. Font, size, position, and everything else is exactly the same. Approve this version?"
            elif "date" in feedback_lower or "time" in feedback_lower:
                return "Updated! Only the date/time changed. Everything else is exactly the same. Approve this version?"
            elif "phone" in feedback_lower or "number" in feedback_lower:
                return "Updated! Only the phone number changed. Everything else is exactly the same. Approve this version?"
            elif "typo" in feedback_lower or "spelling" in feedback_lower or "fix" in feedback_lower:
                return "Fixed! Only the text correction was made. Everything else is exactly the same. Approve this version?"
            else:
                return "Updated! I changed the text as requested. Everything else is exactly the same. Approve this version?"

        elif edit_category == "style_edit":
            # Customize based on what style property changed
            if "background" in feedback_lower:
                if "darker" in feedback_lower or "dark" in feedback_lower:
                    return "Done! Background is darker now. All text, layout, and elements untouched. Look good?"
                elif "lighter" in feedback_lower or "light" in feedback_lower:
                    return "Done! Background is lighter now. All text, layout, and elements untouched. Look good?"
                else:
                    return "Done! Background changed. All text, layout, and elements untouched. Look good?"
            elif "colour" in feedback_lower or "color" in feedback_lower:
                return "Done! Colours adjusted. Layout, text content, and everything else preserved. Look good?"
            elif "bigger" in feedback_lower or "larger" in feedback_lower or "size" in feedback_lower:
                return "Done! Resized as requested. Position, layout, and everything else untouched. Look good?"
            elif "position" in feedback_lower or "move" in feedback_lower:
                return "Done! Repositioned the element. Everything else stays in place. Look good?"
            else:
                return "Done! Visual style adjusted. Layout, text, and content preserved. Look good?"

        elif edit_category == "content_edit":
            # Customize based on add/remove action
            if "add" in feedback_lower:
                return "Added! I matched the new element to your existing style. All other elements preserved. Approve?"
            elif "remove" in feedback_lower:
                return "Removed! The space blends naturally with the rest of the design. All other elements preserved. Approve?"
            elif "replace" in feedback_lower or "swap" in feedback_lower:
                return "Replaced! The new element matches your existing style. All other elements preserved. Approve?"
            else:
                return "Modified as requested! I matched it to the existing style. All other elements preserved. Approve?"

        elif edit_category == "full_redesign":
            return "Here's a completely new design. Fresh concept, same brand. Approve?"

        return "Edit complete! Approve?"

    @staticmethod
    async def save_image_version(
        db,
        draft_id: str,
        version_number: int,
        image_url: str,
        edit_category: str,
        edit_feedback: str
    ) -> None:
        """
        Save image version to version history
        PRD Section 5.2: Save Version Function
        """
        try:
            # Mark all existing versions as not current
            await db["image_versions"].update_many(
                {"draft_id": draft_id},
                {"$set": {"is_current": False}}
            )

            # Create new version
            version_doc = {
                "id": str(uuid.uuid4()),
                "draft_id": draft_id,
                "version_number": version_number,
                "image_url": image_url,
                "edit_category": edit_category,
                "edit_feedback": edit_feedback,
                "is_current": True,
                "created_at": datetime.utcnow()
            }

            await db["image_versions"].insert_one(version_doc)
            print(f"[VERSION] Saved version {version_number} for draft {draft_id}")

        except Exception as e:
            print(f"[VERSION] Error saving version: {e}")

    @staticmethod
    async def undo_image_edit(db, draft_id: str, user_id: str) -> Dict[str, Any]:
        """
        Restore previous version of image
        PRD Section 5.3: Undo Function
        """
        try:
            # Get current draft
            draft = await db["content_drafts"].find_one(
                {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
            )

            if not draft:
                return UriResponse.error_response("Draft not found")

            current_version = draft.get("image_version", 1)

            if current_version <= 1:
                return UriResponse.get_single_data_response("undo_error", {
                    "message": "This is the original image. There's nothing to undo."
                })

            # Find previous version
            previous_version = await db["image_versions"].find_one({
                "draft_id": draft_id,
                "version_number": current_version - 1
            })

            if not previous_version:
                # Edge case: v1 was never saved (before the fix was deployed)
                # Try to find ANY previous version, or inform user
                all_versions = await db["image_versions"].find(
                    {"draft_id": draft_id}
                ).sort("version_number", -1).to_list(length=10)

                if all_versions:
                    # Use the oldest available version
                    previous_version = all_versions[-1]
                    print(f"[UNDO] Previous version {current_version - 1} not found, using oldest available: v{previous_version['version_number']}")
                else:
                    # No version history at all - this edit was made before version history was working
                    return UriResponse.get_single_data_response("undo_error", {
                        "message": "Version history is not available for this image. This edit was made before the undo feature was enabled. "
                                   "Future edits will support undo."
                    })

            # Restore previous version
            await db["content_drafts"].update_one(
                {"$or": [{"id": draft_id}, {"draft_id": draft_id}]},
                {"$set": {
                    "image_url": previous_version["image_url"],
                    "image_version": previous_version["version_number"],
                    "updated_at": datetime.utcnow()
                }}
            )

            # Update version flags
            await db["image_versions"].update_many(
                {"draft_id": draft_id},
                {"$set": {"is_current": False}}
            )
            await db["image_versions"].update_one(
                {"id": previous_version["id"]},
                {"$set": {"is_current": True}}
            )

            print(f"[UNDO] Reverted draft {draft_id} from v{current_version} to v{previous_version['version_number']}")

            return UriResponse.get_single_data_response("undo_complete", {
                "image_url": previous_version["image_url"],
                "version": previous_version["version_number"],
                "message": "Done! I've reverted to the previous version. Here's what it looked like before the last edit."
            })

        except Exception as e:
            print(f"[UNDO] Error: {e}")
            return UriResponse.error_response(f"Undo failed: {str(e)}")

    @staticmethod
    async def edit_image_for_draft(
        draft_id: str,
        user_id: str,
        feedback: str,
        db,
        force_category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Edit image in-place using user feedback
        PRD Section 4.3: Master Edit Router

        This is the main entry point for image editing.
        Handles classification, credit checks, prompt building, and API calls.

        Args:
            draft_id: Draft ID to edit
            user_id: User making the request
            feedback: User's edit request
            db: Database connection

        Returns:
            UriResponse with edited image or error/warning
        """
        try:
            # Step 1: Get the draft
            draft = await db["content_drafts"].find_one(
                {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
            )

            if not draft:
                return UriResponse.error_response("Draft not found")

            # Get the actual draft ID from the document (could be 'id', 'draft_id', or '_id')
            actual_draft_id = draft.get("id") or draft.get("draft_id") or str(draft.get("_id"))

            # Step 2: Check if this is an undo request
            if ImageEditingService.is_undo_request(feedback):
                return await ImageEditingService.undo_image_edit(db, actual_draft_id, user_id)

            # Step 3: Classify the edit intent (or use forced category from quick buttons)
            if force_category and force_category in ['text_edit', 'style_edit', 'content_edit', 'full_redesign']:
                edit_category = force_category
                print(f"[EDIT] Using forced category: {force_category} (skipped classifier)")
            else:
                edit_category = await ImageEditingService.classify_edit_intent(feedback)

            # Step 4: Check credit implications
            current_version = draft.get("image_version", 1)
            content_edit_count = draft.get("content_edit_count", 0)

            # Full redesign always costs 1 credit
            if edit_category == 'full_redesign':
                has_credits = await credit_service.check_sufficient_credits(user_id, 1)
                if not has_credits:
                    return UriResponse.get_single_data_response("credit_warning", {
                        "message": "Redesigning from scratch costs 1 additional campaign credit. "
                                   "You've used all your credits this month. Upgrade or wait for reset.",
                        "edit_category": edit_category,
                        "credits_required": 1
                    })

                # PRD Section 7.1: Suggest specific edits before redesigning (if not forced)
                # Only suggest if this is NOT from the "Redesign" quick button
                if not force_category:
                    return UriResponse.get_single_data_response("suggest_edit_first", {
                        "message": "Before I start over (which costs 1 credit), would you like me to try a specific change first? "
                                   "For example, I can change the colours, adjust the text, or modify the layout — all free of charge. "
                                   "What specifically don't you like about the current version?",
                        "edit_category": edit_category,
                        "credits_required": 1
                    })

                # Ask for confirmation before charging (when forced from quick button)
                return UriResponse.get_single_data_response("confirm_redesign", {
                    "message": "Redesigning from scratch costs 1 additional campaign credit. "
                               "Want me to proceed, or would you like to try a specific edit instead?",
                    "edit_category": edit_category,
                    "credits_required": 1
                })

            # Content edits: First one free, then 1 credit each
            if edit_category == 'content_edit' and content_edit_count >= 1:
                has_credits = await credit_service.check_sufficient_credits(user_id, 1)
                if not has_credits:
                    return UriResponse.get_single_data_response("credit_warning", {
                        "message": "I've already made one content change to this image. "
                                   "Another content edit costs 1 additional credit. "
                                   "You have 0 credits remaining. Text and colour changes are still free.",
                        "edit_category": edit_category,
                        "credits_required": 1
                    })

                # Warn about credit cost for 2nd+ content edit
                return UriResponse.get_single_data_response("content_edit_warning", {
                    "message": "I've already made one content change to this image. "
                               "Another content edit costs 1 additional credit. "
                               "Text and colour changes are still free. Proceed?",
                    "edit_category": edit_category,
                    "credits_required": 1
                })

            # Step 5: Build the edit prompt based on category
            if edit_category == 'text_edit':
                edit_prompt = ImageEditingService.build_text_edit_prompt(feedback)
            elif edit_category == 'style_edit':
                edit_prompt = ImageEditingService.build_style_edit_prompt(feedback)
            elif edit_category == 'content_edit':
                edit_prompt = ImageEditingService.build_content_edit_prompt(feedback)
            else:  # full_redesign
                # Full redesign uses standard regeneration (not edit API)
                from .image_content_service import ImageContentService
                return await ImageContentService.regenerate_image_for_draft(
                    draft_id=draft_id,
                    user_id=user_id,
                    feedback=feedback,
                    db=db
                )

            # Step 6: Validate we have the original image
            current_image_url = draft.get("image_url")
            if not current_image_url:
                return UriResponse.error_response("No current image found for this draft")

            # Step 6.5: Save the CURRENT version to history (if this is the first edit)
            # This ensures we can undo back to the original
            if current_version == 1:
                # Check if v1 already exists in history
                existing_v1 = await db["image_versions"].find_one({
                    "draft_id": actual_draft_id,
                    "version_number": 1
                })

                if not existing_v1:
                    # Save the original image as v1 before editing
                    print(f"[EDIT] Saving original image as v1 before first edit")
                    await ImageEditingService.save_image_version(
                        db=db,
                        draft_id=actual_draft_id,
                        version_number=1,
                        image_url=current_image_url,
                        edit_category="initial",
                        edit_feedback="Original generated image"
                    )

            # Step 7: Download the current image
            print(f"[EDIT] Downloading image from: {current_image_url}")
            image_bytes = await ImageEditingService._download_image(current_image_url)

            if not image_bytes:
                return UriResponse.error_response("Failed to download current image")

            # Step 8: Call GPT-Image-2 Edit API
            print(f"[EDIT] Calling GPT-Image-2 edit API...")
            platform = draft.get("platform", "instagram")
            # GPT-Image-2 requires dimensions divisible by 16
            # Valid sizes: 1024x1024, 1024x1536, 1536x1024
            size = "1024x1024"  # Default square (divisible by 16)

            edited_image_url = await ImageEditingService._call_edit_api(
                image_bytes=image_bytes,
                prompt=edit_prompt,
                size=size
            )

            if not edited_image_url:
                return UriResponse.error_response("Image edit API call failed")

            # Step 9: Save NEW version to history
            new_version = current_version + 1
            await ImageEditingService.save_image_version(
                db=db,
                draft_id=actual_draft_id,
                version_number=new_version,
                image_url=edited_image_url,
                edit_category=edit_category,
                edit_feedback=feedback
            )

            # Step 10: Update draft with new image
            update_data = {
                "image_url": edited_image_url,
                "image_version": new_version,
                "updated_at": datetime.utcnow()
            }

            # Increment content_edit_count if this is a content edit
            if edit_category == 'content_edit':
                update_data["content_edit_count"] = content_edit_count + 1

                # Deduct credit if this is 2nd+ content edit
                if content_edit_count >= 1:
                    await credit_service.deduct_credit(
                        user_id=user_id,
                        campaign_id=actual_draft_id,
                        reason=f"image_edit_{edit_category}"
                    )

            await db["content_drafts"].update_one(
                {"$or": [{"id": draft_id}, {"draft_id": draft_id}]},
                {"$set": update_data}
            )

            # Step 11: Return success with confirmation message
            print(f"[EDIT] Success! Category: {edit_category}, Version: {new_version}")

            return UriResponse.get_single_data_response("edit_complete", {
                "image_url": edited_image_url,
                "version": new_version,
                "edit_category": edit_category,
                "message": ImageEditingService.get_edit_confirmation_message(edit_category, feedback),
                "credit_charged": edit_category == 'content_edit' and content_edit_count >= 1
            })

        except Exception as e:
            print(f"[EDIT] Error: {e}")
            return UriResponse.error_response(f"Image edit failed: {str(e)}")

    @staticmethod
    async def _download_image(image_url: str) -> Optional[bytes]:
        """
        Download image from URL or decode from base64
        """
        try:
            if image_url.startswith("data:"):
                # Base64 data URL
                import re
                match = re.match(r"data:[^;]+;base64,(.+)", image_url, re.DOTALL)
                if match:
                    return base64.b64decode(match.group(1))
                return None

            elif image_url.startswith("/static/"):
                # Local file path
                import os
                file_path = f"/app{image_url}"
                if os.path.exists(file_path):
                    with open(file_path, "rb") as f:
                        return f.read()
                return None

            else:
                # External URL
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(image_url)
                    if response.status_code == 200:
                        return response.content
                return None

        except Exception as e:
            print(f"[EDIT] Error downloading image: {e}")
            return None

    @staticmethod
    async def _call_edit_api(
        image_bytes: bytes,
        prompt: str,
        size: str = "1024x1024"
    ) -> Optional[str]:
        """
        Call OpenAI images.edit API with GPT-Image-2
        PRD Section 4.3 Step 7: Call GPT Image 2 Edit API
        """
        try:
            from PIL import Image
            import asyncio

            # Convert to PNG format (required by edit API)
            image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

            # Resize to target size
            target_w, target_h = map(int, size.split("x"))
            image = image.resize((target_w, target_h), Image.LANCZOS)

            # Save to PNG buffer
            png_buffer = io.BytesIO()
            image.save(png_buffer, format="PNG")
            png_buffer.seek(0)

            print(f"[EDIT] Calling OpenAI images.edit (size={size})")

            # Call edit API in executor (blocking call)
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: openai_client.images.edit(
                    model="gpt-image-2",
                    image=("image.png", png_buffer, "image/png"),
                    prompt=prompt,
                    n=1,
                    size=size,
                )
            )

            # Get base64 image from response
            if hasattr(response.data[0], 'b64_json') and response.data[0].b64_json:
                b64_data = response.data[0].b64_json
            elif hasattr(response.data[0], 'url'):
                # Download from URL and convert to base64
                async with httpx.AsyncClient() as client:
                    img_response = await client.get(response.data[0].url)
                    b64_data = base64.b64encode(img_response.content).decode()
            else:
                return None

            # Save to local storage
            import uuid
            import os

            filename = f"{uuid.uuid4().hex}.webp"
            static_dir = "/app/static/images"
            os.makedirs(static_dir, exist_ok=True)

            # Convert to WebP for efficient storage
            edited_image = Image.open(io.BytesIO(base64.b64decode(b64_data))).convert("RGB")
            webp_path = f"{static_dir}/{filename}"
            edited_image.save(webp_path, format="WEBP", quality=95, method=6)

            stored_url = f"/static/images/{filename}"
            print(f"[EDIT] Image saved to: {stored_url}")

            return stored_url

        except Exception as e:
            print(f"[EDIT] Edit API call failed: {e}")
            return None
