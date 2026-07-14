"""
Unit tests for ad-creative assembly (creative.py).

Copy-writing, brand-engine image generation, and draft lookups are live (LLM/DB);
here we test the pure parts — assembling copy + image + source into a submittable
creative, the always-on WhatsApp CTA, the copy-only fallback, and the draft→summary
projection used by the "pick from drafts" source.
"""
from app.agents.jane_ads.creative import (
    WHATSAPP_CTA,
    _as_ad_content,
    _draft_to_summary,
    _location_prompt_bit,
    assemble_creative,
)
from app.agents.jane_ads.models import AdCopy, CreativeSource


def test_assemble_with_image_defaults_to_generate_source():
    copy = AdCopy(headline="Fresh Lunch Daily", primary_text="Hot meals near your office.",
                  image_prompt="a bowl of jollof")
    c = assemble_creative(copy, "https://cdn/ad-123.png")
    assert c.image_url == "https://cdn/ad-123.png"
    assert c.headline == "Fresh Lunch Daily"
    assert c.primary_text == "Hot meals near your office."
    assert c.source == CreativeSource.GENERATE
    assert c.generated is True


def test_cta_is_always_whatsapp():
    c = assemble_creative(AdCopy(headline="x"), "https://cdn/a.png")
    assert c.cta == WHATSAPP_CTA == "Send WhatsApp Message"


def test_copy_only_fallback_when_no_image():
    copy = AdCopy(headline="Still Works", primary_text="Copy without an image.")
    c = assemble_creative(copy, None)
    assert c.image_url == ""
    assert c.generated is False          # flagged as copy-only
    assert c.cta == WHATSAPP_CTA         # CTA still attached
    assert c.headline == "Still Works"


def test_empty_image_string_is_fallback():
    c = assemble_creative(AdCopy(headline="h"), "")
    assert c.generated is False


def test_upload_source_is_recorded():
    c = assemble_creative(AdCopy(headline="h"), "https://cdn/user.png", source=CreativeSource.UPLOAD)
    assert c.source == CreativeSource.UPLOAD
    assert c.generated is True


def test_draft_source_is_recorded():
    c = assemble_creative(AdCopy(headline="h"), "https://cdn/draft.png", source=CreativeSource.DRAFT)
    assert c.source == CreativeSource.DRAFT


# ── Draft → summary projection (pure) ──────────────────────────────────────────

def test_draft_to_summary_maps_expected_fields():
    doc = {"id": "d1", "platform": "instagram", "content": "x" * 300,
          "image_url": "https://cdn/d1.png", "created_at": "2026-01-01"}
    s = _draft_to_summary(doc)
    assert s["draft_id"] == "d1"
    assert s["platform"] == "instagram"
    assert s["image_url"] == "https://cdn/d1.png"
    assert len(s["content"]) == 200          # truncated, not raised/dropped


def test_draft_to_summary_falls_back_to_draft_id_field():
    doc = {"draft_id": "legacy1", "image_url": "https://cdn/x.png"}
    assert _draft_to_summary(doc)["draft_id"] == "legacy1"


def test_draft_to_summary_handles_missing_fields():
    s = _draft_to_summary({})
    assert s["draft_id"] == "" and s["content"] == "" and s["image_url"] == ""


# ── Ad-vs-poster framing (pure) ────────────────────────────────────────────────
# The brand engine's internal step chooses POSTER (baked-in headline text) vs PHOTO
# based on how the content string reads. A paid ad must never bake its headline into
# the image — the headline/CTA are separate overlay fields.

def test_as_ad_content_forbids_on_image_text():
    out = _as_ad_content("a bowl of jollof rice on a wooden table")
    assert "no text" in out.lower()
    assert "NOT a poster" in out
    assert "a bowl of jollof rice on a wooden table" in out


def test_as_ad_content_forbids_storefront_signage():
    # A shop signboard with the business name is still on-image text — and can come
    # out garbled — so it must be explicitly banned, not just "no logos/watermarks".
    out = _as_ad_content("a storefront on a busy street")
    assert "sign" in out.lower()


def test_as_ad_content_weaves_in_brand_data():
    out = _as_ad_content("a tailor at work", {
        "brand_voice": "warm and playful", "region": "Lagos", "brand_colors": ["magenta", "gold"],
    })
    assert "warm and playful" in out
    assert "Lagos" in out
    assert "magenta" in out


def test_as_ad_content_handles_no_brand_context():
    out = _as_ad_content("a tailor at work", None)
    assert "a tailor at work" in out
    assert "no text" in out.lower()


# ── Location grounding (pure) ──────────────────────────────────────────────────
# Two failure modes seen in testing: (1) a generic global/Western stock-photo look,
# and (2) overcorrecting into a rundown/rural stereotype for any Nigerian city — a
# developed area (e.g. Ikeja's malls/business district) must not default to that.

def test_location_bit_names_the_specific_city():
    out = _location_prompt_bit("Surulere")
    assert "Surulere" in out
    assert "Nigeria" in out


def test_location_bit_defaults_to_nigeria_without_a_city():
    out = _location_prompt_bit("")
    assert "Nigeria" in out


def test_location_bit_always_rejects_generic_western_look():
    for city in ("Surulere", "", "Lekki"):
        assert "Western" in _location_prompt_bit(city)


def test_location_bit_always_rejects_rundown_stereotype():
    for city in ("Ikeja", "", "Victoria Island"):
        out = _location_prompt_bit(city)
        assert "rundown" in out.lower() or "rural" in out.lower()


def test_location_bit_matches_setting_to_business_tier():
    out = _location_prompt_bit("Ikeja", category="fine dining restaurant")
    assert "fine dining restaurant" in out
    assert "caliber" in out.lower() or "quality" in out.lower()
