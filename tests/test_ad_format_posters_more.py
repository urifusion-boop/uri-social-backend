"""The second poster set — Portrait + Headline (SEED-102), Floating Cards (SEED-103),
Enrol / Sign-up (SEED-104), Billboard Statement (SEED-105), AI Answer (SEED-106) — plus the
pieces they share (tracking, two-tone colours, polygons, paths, tickets, recolouring), Event
Promo's real handles, and Offer Spotlight's occasion tag."""
import asyncio
import io
import json

import pytest
from PIL import Image, ImageDraw

from app.agents.jane_ads import vsg01_orchestrator as orch
from app.agents.jane_ads.ad_formats import (
    ai_answer, billboard_statement, enrol_signup, floating_cards, offer_spotlight, portrait_headline,
)
from app.agents.jane_ads.ad_formats._type import brand_pair, vivid_block
from app.agents.jane_ads.ad_formats.brand_tokens import contrast_ratio, resolve_brand_tokens
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService
from app.utils.fonts import text_width

GREEN = resolve_brand_tokens(["#1B7A3A"])
BLUE = resolve_brand_tokens(["#2F80ED"])
PHOTO = "https://example.com/photo.png"
NEW = ("SEED-102", "SEED-103", "SEED-104", "SEED-105", "SEED-106")


def _types(doc, kind):
    return [l for l in doc["layers"] if l.get("type") == kind or l.get("shape") == kind]


def _texts(doc):
    return [l["content"] for l in _types(doc, "text")]


def _stub_model(monkeypatch, reply):
    async def fake(prompt):
        return reply
    monkeypatch.setattr(orch, "_call_content_model", fake)


def _render(doc) -> Image.Image:
    return Image.open(io.BytesIO(asyncio.run(DocumentRendererService.render_to_png(doc)))).convert("RGB")


# ── Registration ─────────────────────────────────────────────────────────────

def test_all_five_have_builders_corpus_records_and_claims():
    from app.agents.jane_ads.vsg01_corpus_seed import FORMAT_MODULES, build_vsg01_strategies
    ids = {s.strategy_id for s in build_vsg01_strategies()}
    for fid in NEW:
        assert fid in FORMAT_MODULES and fid in orch._BUILDERS and fid in ids
        assert fid in orch.NO_PHOTO_FORMAT_IDS and fid in orch._FORMAT_CLAIMS
    assert len(FORMAT_MODULES) == len(orch._BUILDERS) == 23


def test_portrait_takes_the_owners_photo_but_never_a_customers_or_a_product():
    owner, *_ = orch._vsg01_candidate_params(photo_url=PHOTO, photo_attestation="owner_photo", recomposite=False)
    customer, *_ = orch._vsg01_candidate_params(photo_url=PHOTO, photo_attestation="real_customer_photo",
                                                recomposite=False)
    product, *_ = orch._vsg01_candidate_params(photo_url=PHOTO, photo_attestation="product_photo",
                                               recomposite=False)
    assert "SEED-102" in owner and "SEED-102" not in customer and "SEED-102" not in product


def test_ai_answer_never_carries_a_logo_or_a_real_assistants_name():
    assert ai_answer.FORMAT.brand_mark == "prohibited"
    doc = ai_answer.build_document("how do i stop losing patient files?", "Keep every record in one secure "
                                   "digital system so it is searchable.", "Docerity keeps patient records "
                                   "digital and sends reminders.")
    blob = json.dumps(doc).lower()
    for name in ("chatgpt", "openai", "gemini", "claude", "copilot"):
        assert name not in blob
    assert not _types(doc, "brand_asset")


# ── Portrait + Headline ──────────────────────────────────────────────────────

def test_portrait_has_frame_coloured_word_description_and_two_tone_pill():
    doc = portrait_headline.build_document(
        PHOTO, "Fresh Crops for Healthy Living", "Healthy",
        "We deliver farm-fresh produce grown with care, so every meal is full of goodness.",
        "Shop from us at:", "vfarmers.com", tokens=GREEN, brand_name="Verified Farmers",
        brand_color="#1B7A3A", second_color="#F6C343",
    )
    assert check_legibility(doc, GREEN) == []
    spans = [sp for l in _types(doc, "text") for sp in (l.get("spans") or [])]
    assert any(sp["text"] == "Healthy" and sp["color"] != "#FFFFFF" for sp in spans)
    frame = [l for l in _types(doc, "rounded_rect") if l.get("border_color") and not l.get("fill_color")]
    assert frame and frame[0]["border_width"] >= 2
    texts = _texts(doc)
    assert "vfarmers.com" in texts and "Shop from us at:" in texts
    assert any("farm-fresh" in t for t in texts)  # the description sits beside the last line
    head = [l for l in _types(doc, "text") if l.get("spans")]
    assert min(l["y"] for l in head) >= 1080 * 0.48  # on the person's chest, never their face


def test_portrait_without_a_website_shows_only_the_brand_half():
    doc = portrait_headline.build_document(PHOTO, "Care that keeps records safe", "safe", None, "Book at:", None,
                                           tokens=GREEN, brand_name="Docerity", brand_color="#18A84F")
    assert "Book at:" not in _texts(doc) and "Docerity" in _texts(doc)


# ── Floating Cards ───────────────────────────────────────────────────────────

def test_floating_cards_have_right_aligned_headline_cards_and_arrow_pills():
    doc = floating_cards.build_document(
        PHOTO, "Running out of stock shouldn't surprise you.", "Know what's selling, know what's left.",
        ("Items out of stock", "Restock today"), ("Gold ring", "Low in stock!"),
        "Track your inventory with daash.", "daashapp.co", tokens=BLUE, brand_color="#2F80ED",
        second_color="#F2711C",
    )
    assert check_legibility(doc, BLUE) == []
    assert all(l["text_align"] == "ra" for l in _types(doc, "text") if l["font_size"] >= 60)
    assert len(_types(doc, "polygon")) == 2
    assert {"Restock today", "Low in stock!", "daashapp.co"} <= set(_texts(doc))


def test_bright_orange_stays_orange_with_dark_text_and_blue_deepens_for_white():
    orange, ink = vivid_block("#F2711C")
    assert ink != "#FFFFFF" and contrast_ratio(ink, orange) >= 7
    blue, ink2 = vivid_block("#2F80ED")
    assert ink2 == "#FFFFFF" and contrast_ratio(ink2, blue) >= 7


def test_call_to_action_that_cannot_fit_is_refused():
    with pytest.raises(ValueError):
        floating_cards.build_document(PHOTO, "Short headline here", None, None, None, "Word " * 30, "x.co",
                                      tokens=BLUE)


# ── Enrol / Sign-up ──────────────────────────────────────────────────────────

def test_enrol_sets_capitals_with_bars_button_and_contacts():
    doc = enrol_signup.build_document(
        PHOTO, "Your Future Begins Here", "Campus, Career & Beyond", "Apply for January", "Secure your seat",
        [("phone", "+234 803 555 0101"), ("email", "admissions@metro.edu.ng")], "metro.edu.ng",
        tokens=GREEN, brand_color="#1F8A3B",
    )
    assert check_legibility(doc, GREEN) == []
    texts = _texts(doc)
    assert "CAMPUS, CAREER" in texts or "CAMPUS, CAREER & BEYOND" in texts
    assert "SECURE YOUR SEAT" in texts and "+234 803 555 0101" in texts and "admissions@metro.edu.ng" in texts
    bars = [l for l in _types(doc, "rect") if l["height"] == 9]
    assert bars  # the brand-colour underline under each big line


def test_contacts_are_only_the_businesss_own():
    assert orch._contacts_in("Admissions open. Call 0803 555 0101 or mail hello@school.ng.", {}) == [
        ("phone", "0803 555 0101"), ("email", "hello@school.ng")]
    assert orch._contacts_in("Admissions open now.", {"_whatsapp_number": "2348035550101"}) == [
        ("phone", "+234 803 555 0101")]
    assert orch._contacts_in("Admissions open now.", {}) == []


def test_enrol_is_only_for_a_real_sign_up(monkeypatch):
    _stub_model(monkeypatch, {"is_signup": False})
    assert asyncio.run(orch._content_enrol_signup("B", "c", "We sell shoes")) is None


# ── Billboard Statement ──────────────────────────────────────────────────────

def _billboard_photo(face: str, clutter: bool = False) -> Image.Image:
    im = Image.new("RGB", (1080, 1080), "#5AA0E6")
    d = ImageDraw.Draw(im)
    d.rectangle([300, 220, 780, 820], fill=face)
    if clutter:
        for x in range(300, 780, 30):
            d.rectangle([x, 220, x + 14, 820], fill="#FFFFFF")
    return im


def test_find_panel_locates_a_blank_face_and_measures_its_colour():
    found = billboard_statement.find_panel(_billboard_photo("#1E4FD8"), "#1E4FD8")
    assert found
    (x0, y0, x1, y1), colour = found
    assert abs(x0 - 300) < 12 and abs(x1 - 780) < 12 and abs(y0 - 220) < 12 and abs(y1 - 820) < 12
    assert contrast_ratio(colour, "#1E4FD8") < 1.2


def test_find_panel_refuses_a_cluttered_or_wrongly_coloured_face():
    assert billboard_statement.find_panel(_billboard_photo("#1E4FD8", clutter=True), "#1E4FD8") is None
    assert billboard_statement.find_panel(_billboard_photo("#E8B10C"), "#1E4FD8") is None


def test_billboard_keeps_the_highlight_on_its_own_line_and_reads():
    doc = billboard_statement.build_document(
        PHOTO, (300, 220, 780, 820), "#1855CD", "Posting is not Marketing", "is not",
        "Take your business to the next level.", "next level.", tokens=BLUE, website="infinitemedia.ng",
    )
    head = [l for l in _types(doc, "text") if l["font_size"] >= 54]
    assert [l["content"] for l in head] == ["Posting", "is not", "Marketing"]
    marked = [sp for sp in head[1]["spans"] if sp.get("highlight")]
    assert marked and contrast_ratio(marked[0]["color"], marked[0]["highlight"]) >= 4.5
    face = _types(doc, "rect")[0]
    assert contrast_ratio(head[0]["color"], face["fill_color"]) >= 7
    bg = _types(doc, "ai_generated_background")[0]
    assert bg["recolor"]["to"] == face["fill_color"]
    assert all(l["y"] < 820 for l in _types(doc, "text"))


# ── AI Answer ────────────────────────────────────────────────────────────────

def test_ai_answer_layout_and_overflow():
    doc = ai_answer.build_document("i'm a nigerian student with my course picked. what do i sort out next?",
                                   "Start by mapping your destination and application papers.",
                                   "Goajo offers overseas study support through visa-related steps.")
    texts = _texts(doc)
    assert "Ask anything" in texts and ai_answer.TITLE in texts
    with pytest.raises(ai_answer.TooMuchCopy):
        ai_answer.build_document("why " * 60, "a", "b")


def test_ai_answer_refuses_boasts_and_needs_the_business_named(monkeypatch):
    _stub_model(monkeypatch, {"question": "where do i start?", "advice": "Start with your papers.",
                              "offer": "Goajo is the best study agency in Lagos."})
    assert asyncio.run(orch._content_ai_answer("Goajo", "education", "We help students study abroad")) is None
    _stub_model(monkeypatch, {"question": "where do i start?", "advice": "Start with your papers.",
                              "offer": "We offer overseas study support."})
    assert asyncio.run(orch._content_ai_answer("Goajo", "education", "We help students study abroad")) is None


# ── Content guards shared by the new formats ─────────────────────────────────

def test_floating_cards_drop_figures_the_business_never_gave(monkeypatch):
    _stub_model(monkeypatch, {"headline": "Never run out of stock again", "subline": "Know what's left",
                              "card_one": {"label": "Items out of stock", "value": "6"},
                              "card_two": {"text": "Gold ring", "status": "Low in stock"},
                              "cta_line": "Track your inventory with Daash.", "person": "a woman",
                              "device": "a laptop", "setting": "a stock room"})
    out = asyncio.run(orch._content_floating_cards("Daash", "software", "Inventory tracking for small shops"))
    assert out["card_one"] is None and out["card_two"] == ("Gold ring", "Low in stock")


def test_billboard_highlight_must_be_in_the_statement(monkeypatch):
    _stub_model(monkeypatch, {"statement": "Posting is not marketing", "highlight": "never",
                              "subline": "Grow with us.", "sub_emphasis": "with us."})
    out = asyncio.run(orch._content_billboard_statement("B", "agency", "Marketing for small brands"))
    assert out["highlight"] is None and out["sub_emphasis"] == "with us."


def test_offer_occasion_only_when_the_brief_says_it(monkeypatch):
    reply = {"has_offer": True, "headline": "Get 50% off all our services", "emphasis": "50%",
             "ticket": "50% off", "services": [], "scene": "a woman on a sofa",
             "occasion": "Anniversary Discounted Offer"}
    _stub_model(monkeypatch, reply)
    yes = asyncio.run(orch._content_offer_spotlight("B", "c", "Our anniversary: 50% off all our services"))
    no = asyncio.run(orch._content_offer_spotlight("B", "c", "50% off all our services this week"))
    assert yes["occasion"] == "Anniversary Discounted Offer" and no["occasion"] is None


# ── Event Promo: the brand's real handle ─────────────────────────────────────

HANDLES = {"_social_handles": [{"platform": "instagram", "username": "docerity"},
                               {"platform": "facebook", "username": "docerity"},
                               {"platform": "tiktok", "username": "docerity_ng"}]}


def test_event_uses_the_connected_handle_and_only_its_networks():
    assert orch._event_contact(None, HANDLES) == ("@docerity", ["instagram", "facebook"])
    # A handle in the brief gets only the networks that handle is really on.
    assert orch._event_contact("@docerity_ng", HANDLES) == ("@docerity_ng", ["tiktok"])
    assert orch._event_contact("docerity.com/webinar", HANDLES) == ("docerity.com/webinar", [])
    assert orch._event_contact(None, {"website": "https://www.docerity.com/"}) == ("docerity.com", [])


# ── Shared pieces ────────────────────────────────────────────────────────────

def test_brand_pair_uses_a_real_second_colour_or_a_tint_of_the_first():
    main, second = brand_pair(["#1B7A3A", "#F6C343"], GREEN)
    assert (main, second) == ("#1B7A3A", "#F6C343")
    main, second = brand_pair(["#18A84F"], GREEN)
    assert main == "#18A84F" and second != main and contrast_ratio(second, "#FFFFFF") < 1.6


def test_tracking_tightens_the_measured_and_drawn_width():
    loose = text_width("Healthy Living", "Poppins", 100, 600)
    tight = text_width("Healthy Living", "Poppins", 100, 600, -0.05)
    assert tight == pytest.approx(loose - 0.05 * 100 * 13, abs=0.5)
    doc = {"canvas": {"width": 900, "height": 200, "background_color": "#FFFFFF"}, "layers": [
        {"type": "text", "z_index": 1, "content": "Healthy Living", "x": 10, "y": 40, "font_family": "Poppins",
         "font_size": 100, "font_weight": 600, "color": "#000000", "tracking": -0.05}]}
    img = _render(doc)
    ink = [x for x in range(900) if any(img.getpixel((x, y))[0] < 128 for y in range(40, 160))]
    assert max(ink) - min(ink) < loose - 30


def test_polygon_path_ticket_and_two_line_badge_render():
    doc = {"canvas": {"width": 600, "height": 400, "background_color": "#FFFFFF"}, "layers": [
        {"type": "shape", "shape": "polygon", "z_index": 1, "fill_color": "#2F80ED",
         "points": [[20, 20], [200, 20], [240, 60], [200, 100], [20, 100]]},
        {"type": "shape", "shape": "path", "z_index": 2, "color": "#000000", "stroke_width": 6, "smooth": True,
         "points": [[20, 300], [150, 250], [300, 320], [580, 260]]},
        {"type": "badge", "style": "ticket", "z_index": 3, "text": "50% Off\nAll Services", "cx": 420, "cy": 120,
         "rotation": 20, "fill_color": "#0B6E4F", "text_color": "#FFFFFF", "font_family": "Poppins",
         "font_size": 36},
        {"type": "badge", "z_index": 4, "text": "Anniversary\nDiscounted Offer", "cx": 140, "cy": 200,
         "rotation": -10, "fill_color": "#101512", "text_color": "#FFFFFF", "font_family": "Poppins",
         "font_size": 24},
        # A shape running off the edge is clipped, not an error.
        {"type": "shape", "shape": "path", "z_index": 5, "color": "#12C98B", "stroke_width": 40,
         "points": [[-50, 380], [300, 450], [650, 350]]},
    ]}
    img = _render(doc)
    assert img.getpixel((100, 60))[2] > 200                      # polygon filled blue
    assert img.getpixel((420, 120)) != (255, 255, 255)           # ticket drawn
    assert img.getpixel((140, 200)) != (255, 255, 255)           # two-line tag drawn


def test_recolor_shifts_only_the_face_colour():
    from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService as R
    im = Image.new("RGBA", (100, 100), "#5AA0E6")
    ImageDraw.Draw(im).rectangle([20, 20, 80, 80], fill="#1E4FD8")
    ImageDraw.Draw(im).rectangle([45, 20, 55, 80], fill="#222222")   # a lamp arm across the face
    out = R._recolor(im, {"from": "#1E4FD8", "to": "#123A9E", "box": [10, 10, 90, 90], "tolerance": 44})
    assert out.getpixel((30, 30))[:3] == (0x12, 0x3A, 0x9E)
    assert out.getpixel((50, 50))[:3] == (0x22, 0x22, 0x22)             # the arm keeps its colour
    assert out.getpixel((5, 5))[:3] == (0x5A, 0xA0, 0xE6)               # sky untouched


# ── layout fixes found when testing every format end to end (19 Nov) ─────────

def test_floating_cards_subline_clears_the_headlines_descenders():
    from app.agents.jane_ads.ad_formats import floating_cards
    from app.utils.fonts import load_font
    doc = floating_cards.build_document(
        "https://example.com/scene.png", "Running out of stock shouldn't surprise you.",
        "Know what's selling, know what's left.", ("Items out of stock", "Restock today"),
        ("Low in stock!", "Asymmetric gold ring"), "Track your inventory with daash.", "daashapp.co",
        tokens=resolve_brand_tokens(["#1155CC"]), brand_color="#1155CC", second_color="#F28C38")
    texts = [l for l in doc["layers"] if l.get("type") == "text"]
    head = [l for l in texts if not l.get("secondary") and l["content"].strip() and l["font_size"] >= 60]
    sub = next(l for l in texts if l["content"].startswith("Know what"))
    last = head[-1]
    ink_bottom = last["y"] + load_font(last["font_family"], last["font_size"], last["font_weight"]).getbbox(
        last["content"], anchor="la")[3]
    sub_ink_top = sub["y"] + load_font(sub["font_family"], sub["font_size"], sub["font_weight"]).getbbox(
        sub["content"], anchor="la")[1]
    assert sub_ink_top - ink_bottom >= 12


def _event_with_host(name, role):
    from app.agents.jane_ads.ad_formats import event_promo
    return event_promo.build_document(
        "Master your money mindset", "5-Day WhatsApp Challenge", "19th – 23rd February, 7pm",
        tokens=resolve_brand_tokens(["#F5C400"]), brand_color="#F5C400",
        host_cutout_url="https://example.com/host.png", host_name=name, host_role=role,
        action_line="Join free", contact_line="@brandco")


def _host_photo(doc):
    return next(l for l in doc["layers"] if l.get("type") == "composited_product")


def test_a_short_host_name_keeps_the_photo_where_it_was():
    photo = _host_photo(_event_with_host("Ada Obi", "Money coach"))
    assert photo["x"] == int(1080 * 0.525)


def test_a_long_host_name_moves_the_photo_clear_of_it_and_keeps_its_right_edge():
    short = _host_photo(_event_with_host("Ada Obi", "Money coach"))
    long_ = _host_photo(_event_with_host("Patience Carew", "Certified money mindset coach"))
    assert long_["x"] > short["x"]
    assert long_["x"] + long_["width"] == short["x"] + short["width"]


def test_a_long_host_title_is_shown_in_full_not_cut_at_two_lines():
    doc = _event_with_host("Patience Carew", "Certified money mindset coach and author of Rich Habits")
    shown = " ".join(l["content"] for l in doc["layers"] if l.get("type") == "text" and l.get("font_size") == 28)
    assert "Rich Habits" in shown


def test_portrait_headline_description_always_ends_clear_of_the_pill():
    # Found live on staging: a four-line description ran down to within a few pixels of the CTA pill.
    from app.agents.jane_ads.ad_formats import portrait_headline
    from app.utils.fonts import load_font
    doc = portrait_headline.build_document(
        "https://example.com/p.png", "Secure Digital Patient Records", "Secure",
        "Docerity keeps every patient record digital, searchable, and secure for private clinics in Lagos.",
        "Book a demo at:", "docerity.com", tokens=resolve_brand_tokens(["#18F04F"]), brand_color="#18F04F",
        second_color="#A3145C")
    texts = [l for l in doc["layers"] if l.get("type") == "text"]
    sentence = "Docerity keeps every patient record digital, searchable, and secure for private clinics in Lagos."
    desc = [l for l in texts if l.get("secondary") and l["content"].strip() and l["content"].strip() in sentence]
    pill = min(l["y"] for l in doc["layers"] if l.get("shape") == "rounded_rect" and l.get("height", 0) == 112)
    assert desc, "the description should still be shown"
    last = desc[-1]
    ink_bottom = last["y"] + load_font(last["font_family"], last["font_size"], last["font_weight"]).getbbox(
        last["content"], anchor="la")[3]
    assert ink_bottom <= pill - 12
