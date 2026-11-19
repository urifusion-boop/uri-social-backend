"""Starter Pack's labels fit one line under their photo — a long one used to fail the whole format
(live on staging: 'Your digital health assistant' wraps to 3 lines at 301px) and another style was
used instead."""
import asyncio

from app.agents.jane_ads import vsg01_orchestrator as orch
from app.agents.jane_ads.ad_formats import starter_pack

ITEMS = [("a stethoscope", "Stethoscope"), ("a laptop", "Laptop"), ("a planner", "Planner"),
         ("a coffee mug", "Coffee"), ("a phone", "Phone"), ("a pen", "Pen")]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_a_long_label_gets_one_shorter_rewrite_and_the_pack_is_kept(monkeypatch):
    asked = []

    async def fake_model(prompt):
        asked.append(prompt)
        return {"labels": ["Health app"]}

    monkeypatch.setattr(orch, "_call_content_model", fake_model)
    out = _run(orch._fit_starter_labels("Docerity", "Healthcare software", "Your digital health assistant", ITEMS))
    assert out and out["product_label"] == "Health app" and len(out["items"]) == 6
    assert len(asked) == 1 and "Your digital health assistant" in asked[0]
    cell = starter_pack.cell_width_for(7)
    assert all(starter_pack.label_fits(l, cell) for l in [out["product_label"]] + [i[1] for i in out["items"]])


def test_labels_that_already_fit_cost_no_extra_call(monkeypatch):
    async def fake_model(prompt):
        raise AssertionError("no rewrite needed")

    monkeypatch.setattr(orch, "_call_content_model", fake_model)
    out = _run(orch._fit_starter_labels("Docerity", "Healthcare", "Docerity", ITEMS[:4]))
    assert out["product_label"] == "Docerity" and len(out["items"]) == 4


def test_when_the_rewrite_fails_the_product_label_falls_back_to_the_business_name(monkeypatch):
    async def fake_model(prompt):
        return None

    monkeypatch.setattr(orch, "_call_content_model", fake_model)
    out = _run(orch._fit_starter_labels("Docerity", "Healthcare", "Your digital health assistant", ITEMS))
    assert out["product_label"] == "Docerity"


def test_the_prompt_states_the_real_label_limit():
    import inspect
    assert "_STARTER_LABEL_CHARS" in inspect.getsource(orch._content_starter_pack)


def test_the_pack_has_its_identity_title_and_the_logo_sits_in_the_title_band():
    doc = starter_pack.build_document(
        ["https://e.com/1.png", "https://e.com/2.png", "https://e.com/3.png", "https://e.com/4.png"],
        ["Stethoscope", "Planner", "Coffee", "Calendar"], "https://e.com/product.png", "Health app",
        brand_logo_url="https://e.com/logo.png", title="The Lagos clinic owner starter pack", font_family="Poppins")
    texts = [l for l in doc["layers"] if l.get("type") == "text"]
    assert any("starter pack" in l["content"] or "starter" in l["content"] for l in texts)
    logo = next(l for l in doc["layers"] if l.get("role") == "brand_logo")
    cells = [l for l in doc["layers"] if l.get("type") == "composited_product"]
    assert logo["y"] + logo["height"] <= min(c["y"] for c in cells)          # above the grid
    assert all(l.get("font_family") == "Poppins" for l in texts)


def test_the_recompose_flow_gives_styles_the_real_product_photo():
    import inspect
    from app.agents.jane_ads import creative
    src = inspect.getsource(creative)
    i = src.index("photo_url=reference_image_url, photo_attestation=asset_attestation, recomposite=True")
    assert i > 0


def test_the_title_never_leaves_one_word_alone_on_its_second_line():
    doc = starter_pack.build_document(["a", "b", "c", "d"], ["Planner", "Pens", "Phone", "Laptop"], "p", "Health app",
                                      brand_logo_url="l", title="The Lagos clinic starter pack", font_family="Poppins")
    lines = [l["content"] for l in doc["layers"] if l.get("type") == "text" and l.get("font_weight") == 800]
    assert len(lines) == 1 or len(lines[-1].split()) >= 2


def test_every_item_is_shot_on_the_same_backdrop_and_the_product_gets_the_same_tile():
    assert starter_pack.BACKDROP in starter_pack._item_prompt("a laptop")
    doc = starter_pack.build_document(["a", "b", "c", "d"], ["Planner", "Pens", "Phone", "Laptop"], "p", "Health app")
    tiles = [l for l in doc["layers"] if l.get("type") == "shape" and l.get("fill_color") == starter_pack.BACKDROP]
    product = next(l for l in doc["layers"] if l.get("type") == "composited_product" and l["url"] == "p")
    assert len(tiles) == 1 and (tiles[0]["x"], tiles[0]["y"]) == (product["x"], product["y"])
