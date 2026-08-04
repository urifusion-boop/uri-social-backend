"""
Unit tests for ad-creative assembly (creative.py).

Copy-writing, brand-engine image generation, and draft lookups are live (LLM/DB);
here we test the pure parts — assembling copy + image + source into a submittable
creative, the always-on WhatsApp CTA, the copy-only fallback, and the draft→summary
projection used by the "pick from drafts" source.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.jane_ads.creative import (
    WHATSAPP_CTA,
    _as_ad_content,
    _check_leakage,
    _draft_to_summary,
    _leakage_terms,
    _location_prompt_bit,
    _looks_like_video,
    _strip_leaked_terms,
    _zone_a_block,
    assemble_creative,
    creative_from_recomposite,
    generate_ad_image,
    get_brand_context,
    service_area_from_geo,
    write_ad_copy,
    write_ad_copy_for_image,
)
from app.agents.jane_ads.models import AdCopy, CreativeSource, GeoMode, GeoPin, GeoPlan


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_assemble_with_image_defaults_to_generate_source():
    copy = AdCopy(headline="Fresh Lunch Daily", primary_text="Hot meals near your office.",
                  image_prompt="a bowl of jollof")
    c = assemble_creative(copy, "https://cdn/ad-123.png")
    assert c.image_url == "https://cdn/ad-123.png"
    assert c.headline == "Fresh Lunch Daily"
    assert c.primary_text == "Hot meals near your office."
    assert c.source == CreativeSource.GENERATE
    assert c.generated is True
    assert c.is_video is False


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


# ── Video upload support (pure) ────────────────────────────────────────────────
# UPLOAD (and, in rare cases, DRAFT) can carry a video, not just a photo — the CTA/
# copy/source handling is identical either way, only `is_video` differs.

def test_looks_like_video_detects_common_extensions():
    for url in ("https://cdn/ad.mp4", "https://cdn/ad.mov", "https://cdn/ad.webm",
                "https://cdn/ad.mp4?x=1"):
        assert _looks_like_video(url) is True


def test_looks_like_video_false_for_images():
    for url in ("https://cdn/ad.png", "https://cdn/ad.jpg", "https://cdn/ad.webp"):
        assert _looks_like_video(url) is False


def test_assemble_creative_explicit_is_video_true():
    c = assemble_creative(AdCopy(headline="h"), "https://cdn/clip.mp4",
                          source=CreativeSource.UPLOAD, is_video=True)
    assert c.is_video is True
    assert c.source == CreativeSource.UPLOAD
    assert c.generated is True   # a video still counts as a real, submittable creative


def test_assemble_creative_auto_detects_video_when_not_told():
    # Caller didn't say — fall back to guessing from the URL (e.g. a content draft).
    c = assemble_creative(AdCopy(headline="h"), "https://cdn/clip.mov")
    assert c.is_video is True


def test_assemble_creative_explicit_flag_overrides_guess():
    # A .mp4 URL that's explicitly marked non-video (edge case, e.g. a signed URL
    # without an extension) should respect the caller's explicit answer.
    c = assemble_creative(AdCopy(headline="h"), "https://cdn/weird-no-ext", is_video=False)
    assert c.is_video is False


# ── Creative-type reasoning (PRD §4.1) ────────────────────────────────────────

def test_assemble_creative_surfaces_video_recommendation_on_generate():
    copy = AdCopy(headline="h", video_recommended=True,
                  video_recommendation_reason="A founder talking to camera builds trust fast.")
    c = assemble_creative(copy, "https://cdn/ad.png", source=CreativeSource.GENERATE)
    assert c.video_recommendation == "A founder talking to camera builds trust fast."


def test_assemble_creative_no_recommendation_when_not_flagged():
    copy = AdCopy(headline="h", video_recommended=False, video_recommendation_reason="")
    c = assemble_creative(copy, "https://cdn/ad.png", source=CreativeSource.GENERATE)
    assert c.video_recommendation == ""


def test_assemble_creative_recommendation_surfaces_for_still_upload():
    # A static (non-video) upload still gets the pushback (creative brief spec §6.2/
    # §9) — the SHOULD-vs-CAN tension applies to uploads/drafts too, not just GENERATE.
    copy = AdCopy(headline="h", video_recommended=True, video_recommendation_reason="shown")
    c = assemble_creative(copy, "https://cdn/ad.png", source=CreativeSource.UPLOAD)
    assert c.video_recommendation == "shown"


def test_assemble_creative_recommendation_surfaces_for_draft():
    copy = AdCopy(headline="h", video_recommended=True, video_recommendation_reason="shown")
    c = assemble_creative(copy, "https://cdn/ad.png", source=CreativeSource.DRAFT)
    assert c.video_recommendation == "shown"


def test_assemble_creative_recommendation_suppressed_when_already_video():
    # Already a video — nothing left to recommend.
    copy = AdCopy(headline="h", video_recommended=True, video_recommendation_reason="ignored")
    c = assemble_creative(copy, "https://cdn/ad.mp4", source=CreativeSource.UPLOAD, is_video=True)
    assert c.video_recommendation == ""


# ── generate_ad_image: base64 must never reach a Meta ad (live-diagnosed) ──────
# The shared content engine (ImageContentService._generate_platform_image) returns a
# raw base64 data: URL, not a hosted one — every OTHER caller of that engine uploads
# it to Cloudinary first. generate_ad_image didn't, so a real ad launch failed with
# Meta's generic "code=1, unknown error" because Meta's crawler can't fetch a data URI.

def test_generate_ad_image_uploads_base64_result_to_cloudinary():
    fake_engine_result = {"status": True, "responseData": {"image_url": "data:image/webp;base64,AAAA"}}
    with patch(
        "app.agents.social_media_manager.services.image_content_service.ImageContentService._generate_platform_image",
        new=AsyncMock(return_value=fake_engine_result),
    ), patch(
        "app.utils.cloudinary_upload.upload_base64",
        new=AsyncMock(return_value="https://res.cloudinary.com/df8ckaeam/image/upload/v1/uri-social/jane-ads/x.png"),
    ) as mock_upload:
        url = _run(generate_ad_image("a vibrant workspace"))
    assert url == "https://res.cloudinary.com/df8ckaeam/image/upload/v1/uri-social/jane-ads/x.png"
    mock_upload.assert_called_once()
    assert mock_upload.call_args.args[0] == "data:image/webp;base64,AAAA"


def test_generate_ad_image_passes_through_an_already_hosted_url():
    fake_engine_result = {"status": True, "responseData": {"image_url": "https://cdn.example.com/already-hosted.png"}}
    with patch(
        "app.agents.social_media_manager.services.image_content_service.ImageContentService._generate_platform_image",
        new=AsyncMock(return_value=fake_engine_result),
    ), patch("app.utils.cloudinary_upload.upload_base64", new=AsyncMock()) as mock_upload:
        url = _run(generate_ad_image("a vibrant workspace"))
    assert url == "https://cdn.example.com/already-hosted.png"
    mock_upload.assert_not_called()


def test_generate_ad_image_returns_none_when_cloudinary_upload_fails():
    # A base64 data URL is guaranteed to fail Meta's ad creation — better to fall back
    # to copy-only (the established, already-tested failure path) than hand it over.
    fake_engine_result = {"status": True, "responseData": {"image_url": "data:image/webp;base64,AAAA"}}
    with patch(
        "app.agents.social_media_manager.services.image_content_service.ImageContentService._generate_platform_image",
        new=AsyncMock(return_value=fake_engine_result),
    ), patch(
        "app.utils.cloudinary_upload.upload_base64",
        new=AsyncMock(side_effect=Exception("cloudinary down")),
    ):
        url = _run(generate_ad_image("a vibrant workspace"))
    assert url is None


# ── service_area_from_geo (creative brief spec §4) — pure ──────────────────────
# The one place a location legitimately belongs in customer-facing COPY, distinct
# from the raw geo_target/city used for targeting/image-grounding.

def test_service_area_empty_for_non_local():
    geo = GeoPlan(mode=GeoMode.NON_LOCAL, city="Lagos")
    assert service_area_from_geo(geo, fallback_city="Lagos") == ""


def test_service_area_uses_pin_names():
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Lagos",
                  pins=[GeoPin(name="Yaba"), GeoPin(name="Surulere")])
    assert service_area_from_geo(geo) == "Yaba, Surulere"


def test_service_area_caps_at_three_pins():
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, pins=[
        GeoPin(name="A"), GeoPin(name="B"), GeoPin(name="C"), GeoPin(name="D"),
    ])
    assert service_area_from_geo(geo) == "A, B, C"


def test_service_area_falls_back_to_fallback_area():
    geo = GeoPlan(mode=GeoMode.WATERING_HOLE, fallback_area="Lekki-Ajah axis")
    assert service_area_from_geo(geo) == "Lekki-Ajah axis"


def test_service_area_falls_back_to_city():
    geo = GeoPlan(mode=GeoMode.OWN_RADIUS, city="Ibadan")
    assert service_area_from_geo(geo) == "Ibadan"


def test_service_area_falls_back_to_caller_city_when_no_geo():
    assert service_area_from_geo(None, fallback_city="Enugu") == "Enugu"


# ── Leakage check (creative brief spec §4) — pure ──────────────────────────────

def test_leakage_terms_drops_empty_values():
    assert _leakage_terms("Lekki", "", "  ") == ["Lekki"]


def test_check_leakage_detects_case_insensitive_substring():
    leaked = _check_leakage("Lekki Creatives", "for creative professionals",
                            ["Lekki", "creative professionals"])
    assert "Lekki" in leaked
    assert "creative professionals" in leaked


def test_check_leakage_clean_copy_reports_nothing():
    leaked = _check_leakage("Fresh bags, made to order", "Message us on WhatsApp today.",
                            ["Lekki", "creative professionals"])
    assert leaked == []


def test_strip_leaked_terms_removes_and_collapses_whitespace():
    out = _strip_leaked_terms("Lekki creatives, order now", ["Lekki", "creatives"])
    assert "Lekki" not in out
    assert "creatives" not in out
    assert "  " not in out


# ── Zone A block (creative brief spec §2) — pure ───────────────────────────────

def test_zone_a_block_empty_when_nothing_known():
    assert _zone_a_block("", "", "", "messages") == ""


def test_zone_a_block_names_and_prohibits_each_value():
    out = _zone_a_block("Lekki", "creative professionals", "design", "messages")
    assert "Lekki" in out
    assert "creative professionals" in out
    assert "never write about this" in out


# ── write_ad_copy: two-zone brief + leakage check (creative brief spec §2-4) ───
# Live-confirmed bug: a campaign targeting Lekki for "creative professionals"
# produced copy reading "Lekki creatives" — the fix is these three behaviours.

def test_write_ad_copy_never_leaks_geo_or_audience_into_final_copy():
    cases = [
        ("Lekki", "creative professionals", "bags"),
        ("Yaba", "students", "phone accessories"),
        ("Surulere", "young families", "catering"),
    ]
    for geo_target, audience, category in cases:
        clean = AsyncMock(return_value={
            "headline": "Order fresh today", "primary_text": "Message us on WhatsApp to order.",
            "image_prompt": "a product shot", "video_recommended": False,
            "video_recommendation_reason": "",
        })
        with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=clean):
            copy = _run(write_ad_copy(
                "Test Biz", category, city=geo_target,
                brand_context={"target_audience": audience},
            ))
        assert geo_target.lower() not in copy.headline.lower()
        assert geo_target.lower() not in copy.primary_text.lower()
        assert audience.lower() not in copy.headline.lower()
        assert audience.lower() not in copy.primary_text.lower()


def test_write_ad_copy_service_area_appears_geo_target_does_not():
    mock_call = AsyncMock(return_value={
        "headline": "Bags made in Yaba", "primary_text": "I deliver around Yaba — message me on WhatsApp.",
        "image_prompt": "a product shot", "video_recommended": False, "video_recommendation_reason": "",
    })
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock_call):
        copy = _run(write_ad_copy(
            "Test Biz", "bags", city="Lekki", service_area="Yaba",
            brand_context={"target_audience": "creative professionals"},
        ))
    assert "yaba" in copy.primary_text.lower()
    assert "lekki" not in copy.primary_text.lower()
    assert "creative professionals" not in copy.primary_text.lower()


def test_write_ad_copy_retries_once_on_leak_then_accepts_clean_retry():
    leaking = {"headline": "Lekki creatives", "primary_text": "for creative professionals",
              "image_prompt": "p", "video_recommended": False, "video_recommendation_reason": ""}
    clean = {"headline": "Bags made to order", "primary_text": "Message us on WhatsApp.",
            "image_prompt": "p", "video_recommended": False, "video_recommendation_reason": ""}
    mock_call = AsyncMock(side_effect=[leaking, clean])
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock_call):
        copy = _run(write_ad_copy(
            "Test Biz", "bags", city="Lekki",
            brand_context={"target_audience": "creative professionals"},
        ))
    assert mock_call.await_count == 2
    assert copy.headline == "Bags made to order"
    assert "lekki" not in copy.headline.lower()


def test_write_ad_copy_strips_leak_if_retry_still_leaks_never_loops_again():
    always_leaking = {"headline": "Lekki creative professionals", "primary_text": "shop now",
                      "image_prompt": "p", "video_recommended": False, "video_recommendation_reason": ""}
    mock_call = AsyncMock(return_value=always_leaking)
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock_call):
        copy = _run(write_ad_copy(
            "Test Biz", "bags", city="Lekki",
            brand_context={"target_audience": "creative professionals"},
        ))
    assert mock_call.await_count == 2   # one original call + exactly one retry, never more
    assert "lekki" not in copy.headline.lower()
    assert "creative professionals" not in copy.headline.lower()


def test_write_ad_copy_video_recommendation_parsed():
    mock_call = AsyncMock(return_value={
        "headline": "h", "primary_text": "p", "image_prompt": "p",
        "video_recommended": True, "video_recommendation_reason": "movement sells this",
    })
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock_call):
        copy = _run(write_ad_copy("Zumba Studio", "fitness classes"))
    assert copy.video_recommended is True
    assert copy.video_recommendation_reason == "movement sells this"


# ── write_ad_copy_for_image: same leakage/register treatment (upload/draft path) ─

def test_write_ad_copy_for_image_never_leaks_and_parses_video_signal():
    mock_call = AsyncMock(return_value={
        "headline": "Fresh catering, Surulere style", "primary_text": "for young families",
        "video_recommended": True, "video_recommendation_reason": "seeing the food plated sells it",
    })
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock_call):
        copy = _run(write_ad_copy_for_image(
            "a table of Nigerian dishes", "Test Caterer", "catering",
            city="Surulere", brand_context={"target_audience": "young families"},
        ))
    assert "surulere" not in copy.headline.lower()
    assert "young families" not in copy.primary_text.lower()
    assert copy.video_recommended is True
    assert copy.video_recommendation_reason == "seeing the food plated sells it"


def test_write_ad_copy_for_image_retries_once_on_leak():
    leaking = {"headline": "Lekki creatives here", "primary_text": "shop now"}
    clean = {"headline": "Handmade bags", "primary_text": "Message us on WhatsApp."}
    mock_call = AsyncMock(side_effect=[leaking, clean])
    with patch("app.agents.jane_ads.creative._call_ad_copy_model", new=mock_call):
        copy = _run(write_ad_copy_for_image(
            "a shelf of bags", "Test Biz", "bags", city="Lekki",
            brand_context={"target_audience": "creative professionals"},
        ))
    assert mock_call.await_count == 2
    assert copy.headline == "Handmade bags"


# ── creative_from_recomposite (SOURCE 4, creative brief spec §7) ──────────────
# Product truthfulness rule: the real product photo is preserved, only the scene
# around it is regenerated — reuses ImageContentService's existing reference_image
# pipeline, not a new image-generation capability.

def test_creative_from_recomposite_passes_reference_image_through():
    fake_engine_result = {"status": True, "responseData": {"image_url": "https://cdn.example.com/recomposited.png"}}
    with patch(
        "app.agents.social_media_manager.services.image_content_service.ImageContentService._generate_platform_image",
        new=AsyncMock(return_value=fake_engine_result),
    ) as mock_gen, patch(
        "app.agents.jane_ads.creative.describe_ad_image", new=AsyncMock(return_value="a leather bag on a table"),
    ), patch(
        "app.agents.jane_ads.creative.write_ad_copy_for_image",
        new=AsyncMock(return_value=AdCopy(headline="Handmade bags", primary_text="Message us on WhatsApp.")),
    ):
        creative = _run(creative_from_recomposite(
            "Test Biz", "bags", "https://cdn.example.com/my-real-bag.png",
        ))
    assert mock_gen.await_args.kwargs["reference_image"] == "https://cdn.example.com/my-real-bag.png"
    assert creative.source == CreativeSource.RECOMPOSITE
    assert creative.asset_path == CreativeSource.RECOMPOSITE
    assert creative.image_url == "https://cdn.example.com/recomposited.png"


def test_creative_from_recomposite_falls_back_to_reference_image_on_generation_failure():
    with patch(
        "app.agents.social_media_manager.services.image_content_service.ImageContentService._generate_platform_image",
        new=AsyncMock(return_value={"status": False, "error": "boom"}),
    ), patch(
        "app.agents.jane_ads.creative.describe_ad_image", new=AsyncMock(return_value=""),
    ), patch(
        "app.agents.jane_ads.creative.write_ad_copy",
        new=AsyncMock(return_value=AdCopy(headline="Handmade bags", primary_text="Message us on WhatsApp.")),
    ):
        creative = _run(creative_from_recomposite(
            "Test Biz", "bags", "https://cdn.example.com/my-real-bag.png",
        ))
    # Product truthfulness: never lose the real product photo, even if regeneration fails.
    assert creative.image_url == "https://cdn.example.com/my-real-bag.png"


# ── Attribute tagging (creative brief spec §11) — computed at assembly, pure ──

def test_assemble_creative_shows_price_true_when_naira_sign_present():
    copy = AdCopy(headline="Bags from ₦12,000", primary_text="Message us on WhatsApp.")
    c = assemble_creative(copy, "https://cdn/ad.png")
    assert c.shows_price is True


def test_assemble_creative_shows_price_false_without_naira_sign():
    copy = AdCopy(headline="Bags for less", primary_text="Message us on WhatsApp.")
    c = assemble_creative(copy, "https://cdn/ad.png")
    assert c.shows_price is False


def test_assemble_creative_shows_service_area_true_when_present():
    copy = AdCopy(headline="Handmade bags", primary_text="I deliver around Yaba — message me.")
    c = assemble_creative(copy, "https://cdn/ad.png", service_area="Yaba")
    assert c.shows_service_area is True


def test_assemble_creative_shows_service_area_false_when_absent():
    copy = AdCopy(headline="Handmade bags", primary_text="Message me on WhatsApp.")
    c = assemble_creative(copy, "https://cdn/ad.png", service_area="Yaba")
    assert c.shows_service_area is False


def test_assemble_creative_copy_length_matches_combined_text():
    copy = AdCopy(headline="Hi", primary_text="There")
    c = assemble_creative(copy, "https://cdn/ad.png")
    assert c.copy_length == len("Hi There")


def test_assemble_creative_asset_path_mirrors_source():
    c = assemble_creative(AdCopy(headline="h"), "https://cdn/ad.png", source=CreativeSource.RECOMPOSITE)
    assert c.asset_path == CreativeSource.RECOMPOSITE


def _profile_resp(**profile_fields):
    return {"responseData": {"brand_name": "Test Brand", "industry": "retail", **profile_fields}}


def test_get_brand_context_picks_a_style_from_the_brands_own_rotation():
    # Live-diagnosed real gap: organic content always injects a style_slug (the
    # brand's own configured rotation, or a sensible default) before generating —
    # ads never did, silently falling back to the content engine's generic
    # "immersive" composition with no style_desc at all. Same selection organic
    # content uses, so an ad image is as considered as a normal post.
    with patch("app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
               new=AsyncMock(return_value=_profile_resp(style_selections=["street_editorial", "trust_builder"]))):
        bc = _run(get_brand_context("u1", db=object(), brand_id="brnd_1"))
    assert bc["style_slug"] == "street_editorial"
    assert bc["style_prompt_fragment"]


def test_get_brand_context_falls_back_to_a_sensible_default_style_with_no_selections():
    with patch("app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
               new=AsyncMock(return_value=_profile_resp(style_selections=[]))):
        bc = _run(get_brand_context("u1", db=object(), brand_id="brnd_1"))
    assert bc["style_slug"] == "trust_builder"
    assert bc["style_prompt_fragment"]


def test_get_brand_context_respects_the_brands_stored_rotation_index():
    with patch("app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
               new=AsyncMock(return_value=_profile_resp(
                   style_selections=["street_editorial", "trust_builder"], style_rotation_index=1,
               ))):
        bc = _run(get_brand_context("u1", db=object(), brand_id="brnd_1"))
    assert bc["style_slug"] == "trust_builder"


def test_get_brand_context_returns_empty_dict_with_no_profile():
    with patch("app.agents.social_media_manager.services.brand_profile_service.BrandProfileService.get",
               new=AsyncMock(return_value={"responseData": None})):
        bc = _run(get_brand_context("u1", db=object(), brand_id="brnd_1"))
    assert bc == {}
