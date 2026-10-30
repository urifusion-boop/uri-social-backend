"""A budget the client types is a budget they stated — including the same amount as
last time, written with a comma, in an opening message that ends with a full stop."""
from app.agents.jane_ads.jane_consultant import (
    _budget_grounded, _build_budget_confirmation_note, _latest_user_reply,
)

BRIEF = "Run an ad for Uri Social in Lagos, ₦10,000 total over 5 days. Send people to our website urisocial.com."


def test_the_latest_reply_ignores_a_trailing_full_stop_and_dots_in_links():
    assert _latest_user_reply(BRIEF) == "send people to our website urisocial.com"
    assert _latest_user_reply("Fashion brand. 10000, ikeja") == "10000, ikeja"


def test_an_opening_message_naming_last_times_budget_is_not_asked_again():
    note = _build_budget_confirmation_note(10000, BRIEF, [])
    assert "Set budget_ngn to 10000" in note


def test_a_comma_formatted_budget_counts_as_said():
    assert _budget_grounded(10000, 10000, BRIEF, [])


def test_the_remembered_budget_alone_still_does_not_count():
    assert not _budget_grounded(10000, 10000, "Run an ad for Uri Social in Lagos.", [])


def test_yes_to_a_rounded_remembered_budget_counts():
    from app.agents.jane_ads.history import remembered_budget_ngn
    rec = {"budget_ngn": 45000, "charged_upfront_ngn": 49999.99}
    known = remembered_budget_ngn([rec])
    assert known == 50000
    history = [{"role": "assistant",
                "content": "How much would you like to spend? Last time you spent ₦50,000 — want to do the same again?"}]
    assert _budget_grounded(known, known, "promote uri. Lagos and Abuja. YES", history)
    assert "Set budget_ngn to 50000" in _build_budget_confirmation_note(known, "promote uri. YES", history)
