"""
V2-only brand preferences: style_family + logo control mode.

Deliberately NOT stored on the shared brand_profiles document or any other V1
file — Visual Engine V2 is being built and tested as a fully separate system
before any decision is made to integrate it, so every V2-specific preference
gets its own collection here instead of a new field bolted onto V1's schema.
"""
from typing import Any, Dict, List, Optional
import hashlib
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.agents.visual_engine_v2.services.style_classifier import classify_style_family

COLLECTION = "visual_engine_v2_brand_prefs"

VALID_LOGO_MODES = {"agent", "user"}
VALID_LOGO_POSITIONS = {
    "top_left", "top_right", "top_center",
    "bottom_left", "bottom_right", "bottom_center", "center",
}


def _selections_hash(style_selections: Optional[List[str]]) -> str:
    key = ",".join(sorted(style_selections or []))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class BrandPrefsServiceV2:
    """
    Get/derive/update the V2-only preferences for a brand:

    - style_family: auto-derived from the brand's own style_selections (V1,
      read-only) via the heuristic classifier, cached here, and re-derived
      whenever those selections change UNLESS the user has explicitly
      overridden it via update_prefs(style_family=...).
    - logo_control_mode: "agent" (default) = Orshot renders the logo natively
      in its template slot. "user" = Orshot renders without a logo and V2
      composites it afterward at the user's exact chosen position.
    """

    @staticmethod
    async def get_or_create(
        db: AsyncIOMotorDatabase,
        user_id: str,
        brand_id: str,
        style_selections: Optional[List[str]] = None,
        industry: Optional[str] = None,
    ) -> Dict[str, Any]:
        doc = await db[COLLECTION].find_one({"user_id": user_id, "brand_id": brand_id})
        current_hash = _selections_hash(style_selections)

        if doc and (doc.get("style_family_override") or doc.get("style_selections_hash") == current_hash):
            return doc

        derived_family = classify_style_family(style_selections, industry)
        update: Dict[str, Any] = {
            "user_id": user_id,
            "brand_id": brand_id,
            "style_family": derived_family,
            "style_selections_hash": current_hash,
            "updated_at": datetime.utcnow(),
        }
        if not doc:
            update["logo_control_mode"] = "agent"
            update["logo_manual_position"] = None
            update["style_family_override"] = False
            update["created_at"] = datetime.utcnow()

        await db[COLLECTION].update_one(
            {"user_id": user_id, "brand_id": brand_id},
            {"$set": update},
            upsert=True,
        )
        return await db[COLLECTION].find_one({"user_id": user_id, "brand_id": brand_id})

    @staticmethod
    async def update_prefs(
        db: AsyncIOMotorDatabase,
        user_id: str,
        brand_id: str,
        logo_control_mode: Optional[str] = None,
        logo_manual_position: Optional[str] = None,
        style_family: Optional[str] = None,
    ) -> Dict[str, Any]:
        update: Dict[str, Any] = {"updated_at": datetime.utcnow()}

        if logo_control_mode is not None:
            if logo_control_mode not in VALID_LOGO_MODES:
                raise ValueError(f"Invalid logo_control_mode: {logo_control_mode}")
            update["logo_control_mode"] = logo_control_mode

        if logo_manual_position is not None:
            if logo_manual_position not in VALID_LOGO_POSITIONS:
                raise ValueError(f"Invalid logo_manual_position: {logo_manual_position}")
            update["logo_manual_position"] = logo_manual_position

        if style_family is not None:
            update["style_family"] = style_family
            update["style_family_override"] = True

        await db[COLLECTION].update_one(
            {"user_id": user_id, "brand_id": brand_id},
            {"$set": update},
            upsert=True,
        )
        return await db[COLLECTION].find_one({"user_id": user_id, "brand_id": brand_id})
