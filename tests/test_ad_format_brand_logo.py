"""The poster formats put their logo where the brand chose (Brand Playbook logo position and size),
and generated artwork with a warm background no longer fails the legibility check (it used to be
dropped for the plain date-card version)."""
from app.agents.jane_ads.ad_formats import event_promo
from app.agents.jane_ads.ad_formats.brand_logo import place_brand_logo
from app.agents.jane_ads.ad_formats.brand_tokens import resolve_brand_tokens

LOGO = "https://example.com/logo.png"


def _event(**kw):
    return event_promo.build_document(
        "Get more customers on Instagram", "Free live webinar on Zoom", "Thursday 4th December, 2pm",
        tokens=resolve_brand_tokens(["#C61E77"]), brand_color="#C61E77", host_name="Ada Obi",
        host_role="social media coach", action_line="Register now", contact_line="urisocial.com/webinar",
        brand_logo_url=LOGO, **kw)


def _logo(doc):
    return next(l for l in doc["layers"] if l.get("role") == "brand_logo")


def test_artwork_with_a_warm_background_is_kept_not_dropped():
    # #E2D5D0 is the beige that failed live on staging (grey text at 6.8:1).
    doc = _event(scene_url="https://example.com/art.png", scene_surface="#E2D5D0")
    assert any(l.get("type") == "ai_generated_background" for l in doc["layers"])


def test_logo_goes_top_left_aligned_left_when_the_brand_chose_top_left():
    doc = place_brand_logo(_event(), {"logo_position": "top_left", "logo_size": "medium"})
    logo = _logo(doc)
    assert logo["align_x"] == 0.0 and logo["x"] < 100 and logo["y"] < 150 and logo["trim"]


def test_logo_goes_top_centre_when_the_brand_chose_it():
    logo = _logo(place_brand_logo(_event(), {"logo_position": "top_center"}))
    assert logo["align_x"] == 0.5 and abs(logo["x"] + logo["width"] / 2 - 540) < 2


def test_logo_size_follows_the_playbook():
    small = _logo(place_brand_logo(_event(), {"logo_position": "top_left", "logo_size": "small"}))
    large = _logo(place_brand_logo(_event(), {"logo_position": "top_left", "logo_size": "large"}))
    assert large["width"] > small["width"]


def test_a_position_the_layout_already_uses_is_passed_over_for_a_clear_one():
    doc = {"canvas": {"width": 1080, "height": 1080}, "layers": [
        {"type": "text", "content": "BIG TITLE HERE", "font_family": "Inter", "font_size": 90, "font_weight": 800,
         "x": 40, "y": 30},
        {"type": "brand_asset", "role": "brand_logo", "url": LOGO, "x": 57, "y": 41, "width": 220, "height": 84},
    ]}
    logo = _logo(place_brand_logo(doc, {"logo_position": "top_left"}))
    assert (logo["x"], logo["y"]) != (57, 41) and logo["y"] > 150 or logo["x"] > 400


def test_the_brands_corner_is_kept_when_it_is_clear():
    logo = _logo(place_brand_logo(_event(), {"logo_position": "bottom_left"}))
    assert logo["x"] < 100 and logo["y"] > 900 and logo["align_x"] == 0.0


def test_a_logo_inside_a_design_element_is_never_moved():
    from app.agents.jane_ads.ad_formats import portrait_headline
    doc = portrait_headline.build_document(
        "https://example.com/p.png", "Secure Patient Records", "Secure", None, "Book a demo at:", "docerity.com",
        tokens=resolve_brand_tokens(["#18F04F"]), brand_color="#18F04F", brand_logo_url=LOGO)
    before = [dict(l) for l in doc["layers"] if l.get("type") == "brand_asset"]
    after = [l for l in place_brand_logo(doc, {"logo_position": "top_left"})["layers"] if l.get("type") == "brand_asset"]
    assert before == after
