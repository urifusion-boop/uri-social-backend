"""
Jane + Ads — per-brand WhatsApp routing.

Click-to-WhatsApp ads that use a Page-connected destination route every chat to the
ONE WhatsApp number connected to that Facebook Page — so on our shared URI Page, every
brand's leads would land in the same inbox, not the brand's own (the bug Precious hit:
"2 conversations" but no message reached her). We instead run wa.me link ads: the ad's
link is https://wa.me/<brand's number>, so leads open a chat straight with the brand —
no per-brand Facebook Page required, just their number.

`normalize_wa_number` is pure and unit-tested; the get/set helpers persist one number
per brand so a returning brand is never asked again.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

SETTINGS_COLLECTION = "jane_ads_brand_settings"

# Nigeria — the only market Jane Ads targets today (adset geo is hard-coded to NG).
_DEFAULT_COUNTRY_CODE = "234"


def normalize_wa_number(raw: str, default_cc: str = _DEFAULT_COUNTRY_CODE) -> Optional[str]:
    """Normalize a user-typed phone number into the digits-only, country-coded form
    wa.me expects (e.g. '2348031234567'). Returns None if it can't be made sense of.

    Handles the shapes Nigerian users actually type:
      '0803 123 4567'      -> '2348031234567'  (local, leading 0 dropped for the CC)
      '+234 803 123 4567'  -> '2348031234567'  (already international)
      '8031234567'         -> '2348031234567'  (CC and leading 0 both omitted)
      '234803...'          -> unchanged         (already country-coded)
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if digits.startswith("0"):
        digits = default_cc + digits[1:]        # local 0803… → 234803…
    elif len(digits) <= 10:
        digits = default_cc + digits            # bare 803… (no CC, no 0) → 234803…
    # else: already carries a country code (234… or another international prefix)
    if not (10 <= len(digits) <= 15):           # E.164 allows at most 15 digits
        return None
    return digits


async def get_brand_whatsapp(db, brand_id: Optional[str]) -> str:
    """The WhatsApp number leads for this brand route to, or '' if none set yet."""
    if not brand_id or db is None:
        return ""
    doc = await db[SETTINGS_COLLECTION].find_one(
        {"brand_id": brand_id}, {"_id": 0, "whatsapp_number": 1}
    )
    return (doc or {}).get("whatsapp_number", "") or ""


async def set_brand_whatsapp(db, brand_id: Optional[str], number: str) -> None:
    """Store (upsert) the brand's WhatsApp number so it's reused on every future
    campaign — the brand is asked for it once, not per campaign."""
    if not brand_id or db is None or not number:
        return
    await db[SETTINGS_COLLECTION].update_one(
        {"brand_id": brand_id},
        {"$set": {"whatsapp_number": number, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
