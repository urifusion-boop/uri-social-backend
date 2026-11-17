"""Event Promo (SEED-099), Offer Spotlight (SEED-100), Problems vs Solution (SEED-101)."""
import asyncio
import io

import pytest
from PIL import Image

from app.agents.jane_ads import vsg01_orchestrator as orch
from app.agents.jane_ads.ad_formats import event_promo, offer_spotlight, problems_vs_solution
from app.agents.jane_ads.ad_formats._type import (
    block_with_text, brand_block_color, emphasis_spans, headline_family, lines_with_emphasis,
)
from app.agents.jane_ads.ad_formats.brand_tokens import contrast_ratio, resolve_brand_tokens
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

YELLOW = resolve_brand_tokens(["#E8B10C"])
GREEN = resolve_brand_tokens(["#12C98B"])
PHOTO = "https://example.com/photo.png"


def _types(doc, kind):
    return [l for l in doc["layers"] if l.get("type") == kind or l.get("shape") == kind]


# ── Event Promo ──────────────────────────────────────────────────────────────

def test_event_with_host_photo_has_date_pill_host_and_button():
    doc = event_promo.build_document(
        "Master your money mindset", "5-Day WhatsApp Challenge", "19th – 23rd February",
        tokens=YELLOW, brand_color="#E8B10C", host_cutout_url=PHOTO, host_name="Patience Ade",
        host_role="Money mindset coach", action_line="Join free", contact_line="@brandco",
    )
    texts = [l["content"] for l in _types(doc, "text")]
    assert "19th – 23rd February" in texts and "Patience Ade" in texts and "Join free" in texts
    assert _types(doc, "composited_product")[0]["fit"] == "contain"
    assert check_legibility(doc, {**YELLOW, "surface": "#FFFFFF"}) == []


def test_event_without_photo_puts_the_date_on_a_card_and_never_a_person():
    doc = event_promo.build_document("Small business tax clinic", "Free live webinar", "Sat 14 March",
                                     tokens=GREEN, brand_color="#12C98B", action_line="Register free")
    assert not _types(doc, "composited_product") and not _types(doc, "ai_generated_background")
    assert any("MARCH" in l["content"] for l in _types(doc, "text"))


def test_pale_brand_colour_becomes_a_block_with_dark_text():
    fill, text = block_with_text("#E8B10C")
    assert fill == "#E8B10C" and contrast_ratio(text, fill) >= 7.0


def test_long_event_title_shrinks_instead_of_failing():
    doc = event_promo.build_document(
        "Bookkeeping and tax readiness masterclass",
        "3-Day live Zoom training", "Mon 2 – Wed 4 March, 10am", tokens=GREEN,
        host_cutout_url=PHOTO, host_name="Ada Obi", host_role="Chartered accountant",
        action_line="Book your seat", contact_line="brandco.ng",
    )
    sizes = {l["font_size"] for l in _types(doc, "text") if l.get("font_weight") == 900}
    assert max(sizes) < 120


def test_title_that_cannot_fit_is_refused_so_another_format_is_used():
    with pytest.raises(event_promo.EventPromoOverflow):
        event_promo.build_document("Supercalifragilisticexpialidocious " * 6, "Webinar", "1 May", tokens=GREEN)


# ── Offer Spotlight ──────────────────────────────────────────────────────────

def test_offer_puts_the_offer_in_colour_on_a_ticket_and_over_a_dense_wash():
    doc = offer_spotlight.build_document(
        PHOTO, "Get 50% off all of our creative services", "50%", "50% off all services",
        ["Graphic design", "Video editing", "Web development"], tokens=GREEN, brand_color="#12C98B",
    )
    spans = [sp for l in _types(doc, "text") for sp in (l.get("spans") or [])]
    emph = [sp for sp in spans if sp["text"] == "50%"]
    assert emph and emph[0]["color"] != "#FFFFFF"
    assert contrast_ratio(emph[0]["color"], offer_spotlight.WASH) >= 4.5
    assert _types(doc, "badge")[0]["text"] == "50% off all services"
    assert check_legibility(doc, GREEN) == []


def test_offer_tags_all_carry_white_text_at_7_to_1():
    doc = offer_spotlight.build_document(PHOTO, "Half price braids this week", "Half price", None,
                                         ["Braids", "Wigs", "Nails", "Lashes", "Pedicure"], tokens=GREEN)
    tags = [l for l in doc["layers"] if l.get("shape") == "rect"]
    assert len(tags) == 5 and all(contrast_ratio("#FFFFFF", t["fill_color"]) >= 7.0 for t in tags)


def test_emphasis_split_across_lines():
    lines = ["Fresh crops for healthy", "living every day"]
    assert lines_with_emphasis(lines, "healthy living") == ["healthy", "living"]
    spans = emphasis_spans("Fresh crops for healthy", "healthy", "#FFFFFF", "#F5C400")
    assert spans[-1] == {"text": "healthy", "color": "#F5C400"}


# ── Problems vs Solution ─────────────────────────────────────────────────────

def test_problems_with_product_shows_product_and_ringed_solution():
    doc = problems_vs_solution.build_document(
        ["Dry skin by noon", "Clogged pores", "Too many steps"], product_cutout_url=PHOTO,
        tokens=resolve_brand_tokens(["#D7261E"]), brand_color="#D7261E",
    )
    assert _types(doc, "ellipse") and _types(doc, "composited_product")
    texts = [l["content"] for l in _types(doc, "text")]
    assert "problems" in texts and "solution" in texts and "Clogged pores" in texts


def test_problems_without_product_lists_outcomes_with_ticks():
    doc = problems_vs_solution.build_document(
        ["Running out of stock", "Counting by hand", "Not knowing what sells"],
        solutions=["Know what's selling", "Low-stock alerts"], tokens=YELLOW, brand_color="#E8B10C",
    )
    assert not _types(doc, "composited_product")
    assert any(l["content"].startswith("✓") for l in _types(doc, "text"))


def test_too_many_long_problems_refuse_rather_than_overlap():
    with pytest.raises(problems_vs_solution.TooMuchCopy):
        problems_vs_solution.build_document(
            ["A very long frustration that keeps going and going for many words"] * 4,
            solutions=["x", "y"], tokens=YELLOW,
        )


# ── Brand font and colour ────────────────────────────────────────────────────

def test_brand_font_used_only_when_it_reads_as_a_headline():
    assert headline_family({"primary_font": "bold_condensed"}, "Poppins") == "Bebas Neue"
    assert headline_family({"primary_font": "script_flow"}, "Poppins") == "Poppins"
    assert headline_family({}, "Poppins") == "Poppins"


def test_block_colour_is_the_brands_most_saturated_even_if_pale():
    assert brand_block_color(["#1A1A1A", "#E8B10C"], YELLOW) == "#E8B10C"
    assert brand_block_color([], YELLOW) == YELLOW["accent"]


# ── Honesty rules in the copy ────────────────────────────────────────────────

def test_invented_figures_are_removed():
    assert orch._drop_invented_figures("Save ₦50,000 and 30% today", "we give 30% off") == "Save and 30% today"


def _stub_model(monkeypatch, reply):
    async def fake(prompt):
        return reply
    monkeypatch.setattr(orch, "_call_content_model", fake)


def test_event_needs_its_date_in_the_business_words(monkeypatch):
    _stub_model(monkeypatch, {"is_event": True, "title": "Tax clinic", "event_kind": "Live webinar",
                              "date_text": "14 March", "action_line": "Register free"})
    assert asyncio.run(orch._content_event_promo("B", "c", "Join our tax webinar soon")) is None


def test_event_drops_unnamed_host_and_unearned_free(monkeypatch):
    _stub_model(monkeypatch, {"is_event": True, "title": "Tax clinic", "event_kind": "Live webinar",
                              "date_text": "14 March", "host_name": "Dr Musa", "action_line": "Join free"})
    out = asyncio.run(orch._content_event_promo("B", "c", "Tax webinar on 14 March for traders"))
    assert out["host_name"] is None and out["action_line"] == "Register now"


def test_offer_with_an_invented_discount_is_refused(monkeypatch):
    _stub_model(monkeypatch, {"has_offer": True, "headline": "Get 50% off everything", "emphasis": "50%",
                              "ticket": "50% off", "services": [], "scene": "a woman on a sofa"})
    assert asyncio.run(orch._content_offer_spotlight("B", "c", "We do braids and wigs in Lekki")) is None


def test_offer_with_a_stated_discount_is_kept(monkeypatch):
    _stub_model(monkeypatch, {"has_offer": True, "headline": "Get 20% off all braids", "emphasis": "20%",
                              "ticket": "20% off braids", "services": ["Braids", "Spaceships"],
                              "scene": "a woman on a sofa"})
    out = asyncio.run(orch._content_offer_spotlight("B", "c", "20% off braids this week. We also do wigs."))
    assert out["emphasis"] == "20%" and out["services"] == ["Braids"]


# ── Which photos reach which format ──────────────────────────────────────────

def test_customer_photo_never_reaches_event_or_problems():
    ids, *_ = orch._vsg01_candidate_params(photo_url=PHOTO, photo_attestation="real_customer_photo",
                                           recomposite=False)
    assert "SEED-099" not in ids and "SEED-101" not in ids


def test_product_photo_is_never_an_event_host():
    ids, *_ = orch._vsg01_candidate_params(photo_url=PHOTO, photo_attestation="product_photo", recomposite=False)
    assert "SEED-099" not in ids and "SEED-101" in ids


def test_owner_photo_can_host_an_event_but_is_not_a_product():
    ids, *_ = orch._vsg01_candidate_params(photo_url=PHOTO, photo_attestation="owner_photo", recomposite=False)
    assert "SEED-099" in ids and "SEED-101" not in ids


def test_all_three_have_builders_and_corpus_records():
    from app.agents.jane_ads.vsg01_corpus_seed import FORMAT_MODULES
    for fid in ("SEED-099", "SEED-100", "SEED-101"):
        assert fid in FORMAT_MODULES and fid in orch._BUILDERS and fid in orch.NO_PHOTO_FORMAT_IDS


# ── Renderer pieces these formats rely on ────────────────────────────────────

def test_badge_gradient_ellipse_and_spans_render():
    doc = {"canvas": {"width": 600, "height": 400, "background_color": "#FFFFFF"}, "layers": [
        {"type": "shape", "shape": "vgradient", "z_index": 1, "x": 0, "y": 200, "width": 600, "height": 200,
         "color": "#000000", "from_alpha": 0, "to_alpha": 255},
        {"type": "shape", "shape": "ellipse", "z_index": 2, "x": 20, "y": 20, "width": 200, "height": 80,
         "border_color": "#000000", "border_width": 4},
        {"type": "badge", "z_index": 3, "text": "50% off", "cx": 400, "cy": 80, "rotation": 15,
         "fill_color": "#CD1B78", "text_color": "#FFFFFF", "font_family": "Poppins", "font_size": 40},
        {"type": "text", "z_index": 4, "content": "Get 50% off", "x": 20, "y": 300, "font_size": 48,
         "font_family": "Poppins", "font_weight": 800, "color": "#FFFFFF",
         "spans": [{"text": "Get "}, {"text": "50%", "color": "#12C98B"}, {"text": " off"}]},
    ]}
    img = Image.open(io.BytesIO(asyncio.run(DocumentRendererService.render_to_png(doc)))).convert("RGB")
    assert img.getpixel((300, 395))[0] < 20               # bottom of the gradient is near black
    assert img.getpixel((300, 205))[0] > 230               # top of it still white
    r, g, b = img.getpixel((400, 80 - 30))
    assert (r, g, b) != (255, 255, 255)                    # badge drawn
    greens = [img.getpixel((x, y)) for x in range(20, 400) for y in range(300, 350)]
    assert any(p[1] > 150 and p[0] < 80 for p in greens)   # the "50%" span is green


def test_date_card_fits_a_long_month_and_puts_the_time_on_its_own_line():
    doc = event_promo.build_document("Instagram growth challenge", "Free 3-day WhatsApp challenge",
                                     "2nd – 4th December, 7pm", tokens=GREEN, action_line="Join now")
    from app.utils.fonts import text_width
    card = [l for l in doc["layers"] if l.get("shape") == "rounded_rect" and l["y"] < 400][0]
    lines = [l for l in doc["layers"] if l.get("type") == "text" and l["x"] == card["x"] + card["width"] // 2]
    assert [l["content"] for l in lines][-1] == "7PM"
    assert all(text_width(l["content"], l.get("font_family"), l["font_size"], 900) <= card["width"] - 56 for l in lines)


def test_offer_ticket_sits_above_the_headline_not_mid_frame():
    doc = offer_spotlight.build_document(PHOTO, "Get 30% off all our services", "30%", "30% off all services",
                                         ["Video editing"], tokens=GREEN)
    ticket = _types(doc, "badge")[0]
    first_line = min(l["y"] for l in _types(doc, "text") if l.get("spans"))
    assert ticket["cy"] < first_line and ticket["cy"] > 1080 * 0.5


def test_offer_services_must_be_named_word_for_word(monkeypatch):
    _stub_model(monkeypatch, {"has_offer": True, "headline": "Get 30% off all our services", "emphasis": "30%",
                              "ticket": "30% off", "services": ["Content creation", "Content management"],
                              "scene": "a woman on a sofa"})
    out = asyncio.run(orch._content_offer_spotlight("B", "c", "30% off social media management and content creation"))
    assert out["services"] == ["Content creation"]


def test_event_without_photo_is_centred_not_bottom_heavy():
    doc = event_promo.build_document("Cut patient paperwork time", "Free live webinar on Zoom",
                                     "Thursday 4th December, 2pm", tokens=GREEN, host_name="Dr Ada Obi",
                                     action_line="Register now", contact_line="docerity.com/webinar")
    ys = [l["y"] for l in doc["layers"] if "y" in l]
    ends = [l["y"] + (l.get("height") or l.get("font_size", 48)) for l in doc["layers"] if "y" in l]
    top_gap, bottom_gap = min(ys) - event_promo.PAD, 1080 - event_promo.PAD - max(ends)
    assert abs(top_gap - bottom_gap) <= 4


def test_offer_scene_prompt_carries_the_representation_block():
    from app.agents.jane_ads.layer2_generation import REPRESENTATION_BLOCK
    assert REPRESENTATION_BLOCK in offer_spotlight.scene_prompt("a clinic owner on a sofa")
