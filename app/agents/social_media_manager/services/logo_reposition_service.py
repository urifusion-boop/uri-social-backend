"""
Manual logo reposition/resize on an already-generated draft image.

Deliberately separate from the existing "Canvas Editor" layered-document
system (canvas_editor.py / layer_extraction_service.py) — that system is
opt-in, off by default, and currently broken for logos specifically (its
logo layer is created with an empty image URL that never gets filled in,
while the real logo stays baked into a locked background layer, so
dragging it moves nothing). This is a fresh, minimal, purpose-built path
instead: a draft generated after this shipped has its logo-free
background AND the exact pixel box the logo landed in saved alongside the
final image (see image_content_service.py's _generate_platform_image and
complete_social_manager.py's _generate_image_bg). Repositioning is then a
single deterministic Pillow paste onto that saved background — no AI
call, so nothing else in the image can change, and it's near-instant.
"""
from typing import Any, Dict, Optional

from app.domain.responses.uri_response import UriResponse


class LogoRepositionService:
    @staticmethod
    async def reposition(
        draft_id: str,
        user_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
        db,
        slide_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        draft = await db["content_drafts"].find_one(
            {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
        )
        if not draft:
            return UriResponse.error_response("Draft not found")

        if slide_index is not None:
            slides = draft.get("slides") or []
            if slide_index < 0 or slide_index >= len(slides):
                return UriResponse.error_response("Invalid slide_index")
            background_url = slides[slide_index].get("background_image_url")
            current_image_url = slides[slide_index].get("image_url")
            current_version = slides[slide_index].get("image_version", 1)
        else:
            background_url = draft.get("background_image_url")
            current_image_url = draft.get("image_url")
            current_version = draft.get("image_version", 1)

        if not background_url:
            return UriResponse.error_response(
                "This post doesn't have a saved background, so its logo can't be "
                "repositioned this way — that only works for content generated "
                "after this feature shipped. Regenerate the post to enable it."
            )

        if width <= 0 or height <= 0:
            return UriResponse.error_response("width and height must be positive")

        from .brand_profile_service import BrandProfileService
        # Scoped via the draft's own stamped brand_id (set at generation time),
        # never the caller's currently-active brand, so a multi-brand account
        # can't accidentally paste one brand's logo onto another brand's post.
        profile_result = await BrandProfileService.get(user_id, db, brand_id=draft.get("brand_id"))
        profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        logo_url = (profile or {}).get("logo_url")
        if not logo_url:
            return UriResponse.error_response("No brand logo configured.")

        from .image_content_service import ImageContentService
        try:
            new_b64 = await ImageContentService.composite_logo_at_position(
                background_url, logo_url, x, y, width, height
            )
        except Exception as e:
            return UriResponse.error_response(f"Could not composite the logo: {e}")

        from app.utils.s3_upload import upload_base64
        new_image_url = await upload_base64(
            f"data:image/webp;base64,{new_b64}", folder="uri-social/content-drafts"
        )

        placement = {"x": x, "y": y, "width": width, "height": height}
        actual_draft_id = draft.get("id") or draft.get("draft_id")

        # Plug into the same version history the chat-edit flow uses, so the
        # existing Undo button correctly covers a logo move too — without
        # this, moving the logo never bumped image_version, so Undo either
        # stayed hidden or (worse, if a version already existed) reverted
        # past this move without ever having recorded it.
        from .image_editing_service import ImageEditingService
        if current_version == 1 and current_image_url:
            existing_v1 = await db["image_versions"].find_one({
                "draft_id": actual_draft_id, "slide_index": slide_index, "version_number": 1
            })
            if not existing_v1:
                prior_placement = (
                    slides[slide_index].get("logo_placement") if slide_index is not None
                    else draft.get("logo_placement")
                )
                await ImageEditingService.save_image_version(
                    db=db, draft_id=actual_draft_id, version_number=1,
                    image_url=current_image_url, edit_category="initial",
                    edit_feedback="Original generated image", slide_index=slide_index,
                    background_image_url=background_url, logo_placement=prior_placement,
                )

        new_version = current_version + 1
        await ImageEditingService.save_image_version(
            db=db, draft_id=actual_draft_id, version_number=new_version,
            image_url=new_image_url, edit_category="logo_reposition",
            edit_feedback=f"Moved logo to x={x}, y={y}, width={width}, height={height}",
            slide_index=slide_index, background_image_url=background_url, logo_placement=placement,
        )

        if slide_index is not None:
            update_fields = {
                f"slides.{slide_index}.image_url": new_image_url,
                f"slides.{slide_index}.logo_placement": placement,
                f"slides.{slide_index}.image_version": new_version,
            }
        else:
            update_fields = {
                "image_url": new_image_url,
                "logo_placement": placement,
                "image_version": new_version,
            }

        await db["content_drafts"].update_one({"id": actual_draft_id}, {"$set": update_fields})

        return UriResponse.get_single_data_response("logo_repositioned", {
            "image_url": new_image_url,
            "logo_placement": placement,
        })
