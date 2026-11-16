"""A read also picks the look (staging feedback 2026-11-16: "can it also pick visual style and text
styles?"): visual styles from the style library and a headline and body font from the font library,
saved with the prompt text image generation reads, exactly as setup saves them."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import openai

from app.agents.brand_intel import look, service
from app.agents.social_media_manager.services import style_library


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


FIELDS = {"brand_name": {"value": "JTF Foundation"}, "industry": {"value": "Nonprofit organization"},
          "derived_voice": {"value": "warm, hopeful"}}


def _resp(content):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=content))]
    return r


def test_only_styles_offered_for_the_industry_and_real_fonts_are_kept():
    candidates = style_library.get_styles_for_industry("Nonprofit organization")
    raw = {"styles": [candidates[1], "made_up_style", candidates[0], candidates[1]],
           "headline_font": "serif_elegance", "body_font": "comic_sans"}
    out = look.clean(raw, candidates, {"url": "https://instagram.com/jtf"})
    assert out["style_selections"]["value"] == [candidates[1], candidates[0]]
    assert out["style_selections"]["basis"] == "inferred"
    assert out["primary_font"]["value"] == "serif_elegance"
    assert "secondary_font" not in out


def test_choices_are_saved_with_the_prompt_text_image_generation_reads():
    styles = style_library.get_styles_for_industry("")[:2]
    upd = look.companions("style_selections", styles, {})
    assert upd["style_prompt_fragments"] == [style_library.get_prompt_fragment(s) for s in styles]
    assert look.companions("primary_font", "modern_sans", {})["primary_font_prompt"] == look.FONTS["modern_sans"]["prompt"]
    assert look.companions("secondary_font", "nope", {}) == {}


def test_a_style_already_chosen_keeps_the_text_it_was_saved_with():
    s = style_library.get_styles_for_industry("")[0]
    upd = look.companions("style_selections", [s], {"style_selections": [s], "style_prompt_fragments": ["mine"]})
    assert upd["style_prompt_fragments"] == ["mine"]


def test_applying_a_style_or_font_saves_its_prompt_too():
    s = style_library.get_styles_for_industry("")[0]
    fields = {"style_selections": {"value": [s], "basis": "inferred", "page": "p"},
              "primary_font": {"value": "bold_condensed", "basis": "inferred", "page": "p"}}
    upd = service.build_update(fields, ["style_selections", "primary_font"], {})
    assert upd["style_selections"] == [s] and upd["style_prompt_fragments"] == [style_library.get_prompt_fragment(s)]
    assert upd["primary_font"] == "bold_condensed"
    assert upd["primary_font_prompt"] == look.FONTS["bold_condensed"]["prompt"]


def test_the_accounts_own_post_images_are_shown_to_the_model():
    s = style_library.get_styles_for_industry("Nonprofit organization")[0]
    create = AsyncMock(return_value=_resp(f'{{"styles": ["{s}"], "headline_font": "modern_sans", "body_font": "modern_sans"}}'))
    client = MagicMock()
    client.chat.completions.create = create
    with patch("app.agents.brand_intel.look.openai.AsyncOpenAI", return_value=client):
        out = _run(look.pick(FIELDS, {"url": "u", "images": ["https://cdn/1.jpg", "http://insecure/2.jpg"]}))
    content = create.await_args.kwargs["messages"][0]["content"]
    assert [c["image_url"]["url"] for c in content if c["type"] == "image_url"] == ["https://cdn/1.jpg"]
    assert out["style_selections"]["value"] == [s] and out["secondary_font"]["value"] == "modern_sans"


def test_an_image_the_model_cant_open_falls_back_to_reading_without_images():
    s = style_library.get_styles_for_industry("")[0]
    bad = openai.BadRequestError("Invalid image URL", response=MagicMock(status_code=400), body=None)
    create = AsyncMock(side_effect=[bad, _resp(f'{{"styles": ["{s}"]}}')])
    client = MagicMock()
    client.chat.completions.create = create
    with patch("app.agents.brand_intel.look.openai.AsyncOpenAI", return_value=client):
        out = _run(look.pick({"industry": {"value": ""}}, {"url": "u", "images": ["https://cdn/1.jpg"]}))
    assert isinstance(create.await_args_list[1].kwargs["messages"][0]["content"], str)
    assert out["style_selections"]["value"] == [s]


def test_a_failure_picking_the_look_never_fails_the_read():
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("down"))
    with patch("app.agents.brand_intel.look.openai.AsyncOpenAI", return_value=client):
        assert _run(look.pick(FIELDS, {"url": "u"})) == {}


def test_the_font_library_matches_the_apps():
    assert len(look.FONTS) == 16 and all(f["prompt"] and f["name"] for f in look.FONTS.values())


# ── editing any suggestion before Use (staging 2026-11-16: "couldn't edit brand colours, CTA, logo") ──

def test_an_edited_list_colours_and_logo_are_the_owners():
    fields = {"cta_styles": {"value": ["DM us", "Learn more"], "basis": "inferred", "page": "p"},
              "brand_colors": {"value": ["#D81818", "#C01818"], "basis": "inferred", "page": "p"},
              "logo_url": {"value": "https://s3/found.png", "basis": "inferred", "page": "p"}}
    edits = {"cta_styles": ["DM us", " Donate  now ", "DM us", ""], "brand_colors": ["#8E1B5C"],
             "logo_url": "https://s3/mine.png"}
    upd = service.build_update(fields, list(fields), {}, edits=edits)
    assert upd["cta_styles"] == ["DM us", "Donate now"]
    assert upd["brand_colors"] == ["#8E1B5C"]
    assert upd["logo_url"] == "https://s3/mine.png"
    assert all(upd["field_sources"][k]["source"] == "you" for k in fields)


def test_an_edit_of_the_wrong_shape_is_ignored():
    fields = {"brand_colors": {"value": ["#D81818"], "basis": "inferred", "page": "p"},
              "logo_url": {"value": "https://s3/found.png", "basis": "inferred", "page": "p"},
              "cta_styles": {"value": ["DM us"], "basis": "inferred", "page": "p"}}
    edits = {"brand_colors": ["red"], "logo_url": "javascript:alert(1)", "cta_styles": "DM us"}
    upd = service.build_update(fields, list(fields), {}, edits=edits)
    assert upd["brand_colors"] == ["#D81818"] and upd["logo_url"] == "https://s3/found.png"
    assert upd["cta_styles"] == ["DM us"]
    assert upd["field_sources"]["brand_colors"]["source"] == "website"


def test_a_chosen_logo_is_stored_but_not_saved_to_the_playbook():
    import io

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image

    from app.agents.brand_intel import router as R
    from app.dependencies import get_active_brand_context

    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[get_active_brand_context] = lambda: {"user_id": "u1", "brand_id": "b1"}
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (142, 27, 92)).save(buf, format="JPEG")
    with patch("app.utils.s3_upload.upload_bytes", new=AsyncMock(return_value="https://s3/chosen.png")) as up:
        client = TestClient(app)
        ok = client.post("/brand-intel/logo", files={"file": ("logo.jpg", buf.getvalue(), "image/jpeg")})
        bad = client.post("/brand-intel/logo", files={"file": ("x.txt", b"not an image", "text/plain")})
    assert ok.status_code == 200 and ok.json() == {"logo_url": "https://s3/chosen.png"}
    assert up.await_count == 1
    assert bad.status_code == 400 and "isn't an image" in bad.json()["detail"]
