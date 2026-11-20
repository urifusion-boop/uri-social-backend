"""Fact-led styles (Event Promo, Offer Spotlight, Enrol / Sign-up, Price-Led Offer, The Receipt,
Review Card, Testimonial + Offer) are flagged BEFORE generation when the brief can't support them —
live on staging Event Promo was picked for a product brief with no event and the client only learnt
afterwards ("Couldn't build Event Promo for this one")."""
import asyncio

from app.agents.jane_ads import vsg01_orchestrator as orch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _model(answer):
    async def fake(prompt):
        return answer
    return fake


def test_a_product_brief_with_no_event_flags_event_promo_with_the_reason(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model({
        "event_with_date": False, "offer": False, "signup": False, "price": False,
        "item_prices": False, "customer_quote": False}))
    out = _run(orch.brief_unmet_needs("Content creation for Nigerian SMEs"))
    assert out["SEED-099"] == "Needs an event with a date in your brief"
    assert set(out) == set(orch.FACT_NEEDS)


def test_a_webinar_brief_with_a_date_does_not_flag_event_promo(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model({
        "event_with_date": True, "offer": False, "signup": True, "price": False,
        "item_prices": False, "customer_quote": False}))
    out = _run(orch.brief_unmet_needs("Free webinar on Zoom, Thursday 4th December, 2pm. Register at uri.ng"))
    assert "SEED-099" not in out and "SEED-104" not in out and "SEED-100" in out


def test_a_failed_check_never_blocks_anything(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model(None))
    assert _run(orch.brief_unmet_needs("anything")) == {}


def test_styles_that_write_their_own_copy_are_never_flagged():
    for fid in ("SEED-101", "SEED-102", "SEED-103", "SEED-105", "SEED-106", "SEED-097"):
        assert fid not in orch.FACT_NEEDS
