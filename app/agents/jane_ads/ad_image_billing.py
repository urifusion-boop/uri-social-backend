"""
Credits for an ad's images, priced by the style it is made in.

Every ad has cost a flat 1 credit, whatever it took to make: a Text-Only ad draws no AI image, a
Starter Pack makes up to six (one per item) plus a cut-out, and any style with people can redo a
scene once. Here each style has a price from the images it needs:

    0-1 images -> 1 credit,   2-3 images -> 2 credits,   4 or more -> 3 credits

Mode (settings.AD_IMAGE_BILLING_MODE, same idea as video's VIDEO_GENERATION_BILLING_MODE):
  off     nothing is recorded; the flat 1 credit as before.
  shadow  (default) the flat 1 credit is still what's charged; every ad's style price and the image
          calls it really made are recorded in `ad_image_billing`, to compare with the OpenAI bill
          before charging by style.
  on      the style's price is charged instead of the flat 1 credit.
"""
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings

COLLECTION = "ad_image_billing"
FLAT_CREDITS = 1

# Image-model calls a style makes for one ad, before any retry.
STYLE_IMAGES: dict[str, int] = {
    "SEED-097": 0, "SEED-106": 0, "SEED-081": 0, "SEED-075": 0, "SEED-087": 0,
    "SEED-093": 0, "SEED-096": 0, "SEED-074": 0, "SEED-101": 0,
    "SEED-099": 1, "SEED-100": 1, "SEED-102": 1, "SEED-103": 1, "SEED-104": 1, "SEED-105": 1,
    "SEED-077": 1, "SEED-098": 1, "SEED-089": 1, "SEED-082": 1, "SEED-083": 1,
    "SEED-080": 2, "SEED-078": 2,
    "SEED-088": 6,
}
# An ad made without a style: one generated image.
GENERIC_IMAGES = 1


def mode() -> str:
    value = (getattr(settings, "AD_IMAGE_BILLING_MODE", "shadow") or "shadow").strip().lower()
    return value if value in ("off", "shadow", "on") else "shadow"


def credits_for_images(images: int) -> int:
    if images <= 1:
        return 1
    if images <= 3:
        return 2
    return 3


def price(format_id: Optional[str]) -> int:
    """The credits an ad in this style costs (a style-less ad: one image)."""
    return credits_for_images(STYLE_IMAGES.get(format_id or "", GENERIC_IMAGES))


def credits_to_charge(format_id: Optional[str]) -> int:
    return price(format_id) if mode() == "on" else FLAT_CREDITS


async def record(db, *, user_id: str, brand_id: Optional[str], campaign_id: str,
                 format_id: Optional[str], image_calls: int, charged: int) -> None:
    """One row per ad: what the style is priced at, what was charged, and the image calls really made.
    Never raises — billing records must not break an ad that was already made."""
    if mode() == "off" or db is None:
        return
    try:
        await db[COLLECTION].insert_one({
            "user_id": user_id, "brand_id": brand_id, "campaign_id": campaign_id,
            "format_id": format_id or "", "mode": mode(),
            "priced_credits": price(format_id), "charged_credits": charged,
            "image_calls": image_calls, "expected_images": STYLE_IMAGES.get(format_id or "", GENERIC_IMAGES),
            "created_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        print(f"[AdImageBilling] record failed: {e}", flush=True)


def credits_needed(forced_format_id: Optional[str]) -> int:
    """What the balance must cover before an ad is generated: the picked style's price when billing
    is on, else the flat credit. When Jane chooses the style, see styles_costing_more_than."""
    return price(forced_format_id) if (mode() == "on" and forced_format_id) else FLAT_CREDITS


def styles_costing_more_than(balance: int) -> list[str]:
    """With billing on and Jane choosing the style, the styles this balance can't pay for: left out of
    her choice, so an ad never costs more than what's left."""
    if mode() != "on":
        return []
    return [fid for fid in STYLE_IMAGES if price(fid) > balance]
