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
    out = _run(orch.brief_unmet_needs("Content creation for Nigerian SMEs", verify=False))
    assert out["SEED-099"] == "Needs an event with a date in your brief"
    assert set(out) == set(orch.FACT_NEEDS)


def test_a_webinar_brief_with_a_date_does_not_flag_event_promo(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model({
        "event_with_date": True, "offer": False, "signup": True, "price": False,
        "item_prices": False, "customer_quote": False}))
    out = _run(orch.brief_unmet_needs("Free webinar on Zoom, Thursday 4th December, 2pm. Register at uri.ng", verify=False))
    assert "SEED-099" not in out and "SEED-104" not in out and "SEED-100" in out


def test_a_failed_check_never_blocks_anything(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model(None))
    assert _run(orch.brief_unmet_needs("anything", verify=False)) == {}


def test_styles_that_write_their_own_copy_are_never_flagged():
    for fid in ("SEED-101", "SEED-102", "SEED-103", "SEED-105", "SEED-106", "SEED-097"):
        assert fid not in orch.FACT_NEEDS


# ── The quick check is confirmed by each style's own content step ───────────────────────────
# Staging, 2026-11-24: a bakery's price list ("Loaf size [8,000], Combo pack [15,000],
# Celebration cakes [15,000 and above]") passed the quick check for The Receipt and Offer
# Spotlight; The Receipt was even Jane's pick, and both fell back to Us vs Them at generation.

BAKERY = ("Loaf size [8,000], Combo pack with yoghurt and a pouch of cookie [15,000] and "
          "Celebration cakes [15,000 and above] — for customers in Ayobo who want banana bread.")

ALL_PRESENT = {"event_with_date": True, "offer": True, "signup": True, "price": True,
               "item_prices": True, "customer_quote": True}


def test_a_style_whose_own_content_step_finds_nothing_is_flagged(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model(ALL_PRESENT))

    async def content(fid, *a, **k):
        return fid not in ("SEED-081", "SEED-100")   # no total for the Receipt, no deal for Offer Spotlight

    monkeypatch.setattr(orch, "_fact_style_content", content)
    out = _run(orch.brief_unmet_needs(BAKERY))
    assert out == {"SEED-081": "Needs items with their prices in your brief",
                   "SEED-100": "Needs a real offer or discount in your brief"}


def test_the_receipt_needs_a_total_not_just_prices(monkeypatch):
    async def receipt(description):
        return {"items": [("Loaf size", "8,000"), ("Celebration cakes", "15,000 and above")],
                "total_label": "Total", "total_amount": ""}

    monkeypatch.setattr(orch, "_content_receipt", receipt)
    assert _run(orch._fact_style_content("SEED-081", "Nayo's", "Bakery", BAKERY, None)) is False


def test_only_the_styles_being_offered_are_verified(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model(ALL_PRESENT))
    asked = []

    async def content(fid, *a, **k):
        asked.append(fid)
        return True

    monkeypatch.setattr(orch, "_fact_style_content", content)
    _run(orch.brief_unmet_needs(BAKERY, only={"SEED-081", "SEED-097"}))
    assert asked == ["SEED-081"]


def test_a_content_step_that_errors_leaves_the_style_available(monkeypatch):
    monkeypatch.setattr(orch, "_call_content_model", _model(ALL_PRESENT))

    async def content(fid, *a, **k):
        raise RuntimeError("model outage")

    monkeypatch.setattr(orch, "_fact_style_content", content)
    assert _run(orch.brief_unmet_needs(BAKERY)) == {}
