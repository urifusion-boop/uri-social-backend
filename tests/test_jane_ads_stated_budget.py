"""
The stated budget vs the ad spend.

Campaign records persist `budget_ngn` as the AD SPEND sent to the platform — the
client's stated budget less URI's fee. Reading it straight back reported a ₦20,000
campaign as ₦18,000, which surfaced in two client-facing places: the campaign card's
BUDGET column, and Jane recalling "your past campaign was around ₦18,000".

The recall case was the damaging one: remembered_budget_ngn also feeds the NEXT
campaign's budget, so every client who accepted "use a similar amount" had their
campaign silently shrunk by 10% again.
"""
from app.agents.jane_ads import constants as C
from app.agents.jane_ads.history import remembered_budget_ngn


def test_charged_amount_wins_when_stamped():
    """charged_upfront_ngn is what actually left the wallet, so it is the truest
    answer and must beat any reconstruction."""
    rec = {"budget_ngn": 18_000, "ad_spend_markup": C.AD_SPEND_MARKUP,
           "charged_upfront_ngn": 20_000}
    assert C.stated_budget_from_record(rec) == 20_000


def test_reconstructed_from_the_stamped_markup_when_not_charged():
    rec = {"budget_ngn": 18_000, "ad_spend_markup": C.AD_SPEND_MARKUP}
    assert C.stated_budget_from_record(rec) == 20_000


def test_legacy_records_use_the_legacy_markup():
    """Records predating the stamp were sold under the old fee model; billing.py bills
    them at LEGACY_AD_SPEND_MARKUP, so the figure shown must match the figure charged
    rather than being re-based onto the current maths."""
    assert C.stated_budget_from_record({"budget_ngn": 18_000}) == round(
        18_000 * C.LEGACY_AD_SPEND_MARKUP, 2
    )


def test_a_record_with_no_budget_is_zero_not_an_error():
    assert C.stated_budget_from_record({}) == 0


def test_the_split_round_trips():
    """ad_spend_from_budget and stated_budget_from_record must be exact inverses, or
    the wallet would not land on zero when the platform finishes spending."""
    for budget in (5_000, 18_000, 20_000, 65_000, 250_000):
        rec = {"budget_ngn": C.ad_spend_from_budget(budget),
               "ad_spend_markup": C.AD_SPEND_MARKUP}
        assert C.stated_budget_from_record(rec) == budget


def test_jane_remembers_the_stated_budget_not_the_ad_spend():
    history = [{"budget_ngn": 18_000, "ad_spend_markup": C.AD_SPEND_MARKUP}]
    assert remembered_budget_ngn(history) == 20_000


def test_reusing_a_remembered_budget_does_not_shrink_the_campaign():
    """The regression this exists for: remembered_budget_ngn feeds the NEXT campaign's
    budget, so returning the ad spend meant each "use a similar amount" cost the client
    10% — ₦20,000 → ₦18,000 → ₦16,200. The figure has to be stable under reuse."""
    budget = 20_000.0
    for _ in range(5):
        record = {"budget_ngn": C.ad_spend_from_budget(budget),
                  "ad_spend_markup": C.AD_SPEND_MARKUP}
        budget = remembered_budget_ngn([record])
        assert budget == 20_000


def test_no_history_remembers_nothing():
    assert remembered_budget_ngn([]) is None


def test_records_without_a_budget_are_skipped():
    history = [{"display_name": "x"}, {"budget_ngn": 18_000, "ad_spend_markup": C.AD_SPEND_MARKUP}]
    assert remembered_budget_ngn(history) == 20_000
