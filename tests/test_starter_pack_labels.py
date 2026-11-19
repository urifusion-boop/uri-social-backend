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
