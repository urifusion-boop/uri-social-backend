"""
Live-diagnosed real bug: a client answered a plain budget question with a bare
number ("10,000") and the consultant's own structured budget_ngn field came back
None on that turn — and every turn after (nothing to backfill from either, since
it was never captured even once). The client had to type the same number twice
before a plan would build. _extract_trailing_bare_budget is a deterministic,
model-independent fallback for exactly this case.
"""
from app.agents.jane_ads.router import _extract_trailing_bare_budget


BUDGET_QUESTION = "Could you let me know the budget you're planning to allocate for this campaign?"
COMPOUND_QUESTION = (
    "Are we aiming for a similar budget this time, or are you looking to adjust it? "
    "Also, could you let me know the city you're targeting?"
)
CITY_ONLY_QUESTION = "Could you let me know the city you're targeting?"


def test_bare_number_reply_to_a_budget_question_is_captured():
    message = "Get me more WhatsApp messages. 10,000"
    assert _extract_trailing_bare_budget(message, BUDGET_QUESTION) == 10000.0


def test_bare_number_reply_to_a_compound_budget_and_city_question_is_captured():
    message = "Get me more WhatsApp messages. 10,000"
    assert _extract_trailing_bare_budget(message, COMPOUND_QUESTION) == 10000.0


def test_naira_symbol_and_commas_are_stripped():
    message = "Get me more WhatsApp messages. ₦10,000"
    assert _extract_trailing_bare_budget(message, BUDGET_QUESTION) == 10000.0


def test_k_suffix_means_thousands():
    message = "Get me more WhatsApp messages. 10k"
    assert _extract_trailing_bare_budget(message, BUDGET_QUESTION) == 10000.0


def test_never_fires_when_the_question_wasnt_about_budget():
    # A bare number replying to a city-only question must NOT be treated as budget —
    # it could be a phone number, a headcount, anything.
    message = "Get me more WhatsApp messages. 500"
    assert _extract_trailing_bare_budget(message, CITY_ONLY_QUESTION) is None


def test_never_fires_on_a_non_numeric_reply():
    # e.g. answering "ikeja" to the compound question above — must not invent a budget.
    message = "Get me more WhatsApp messages. 10,000. ikeja"
    assert _extract_trailing_bare_budget(message, COMPOUND_QUESTION) is None


def test_never_fires_on_a_worded_reply_even_if_it_contains_a_number():
    # "yes 10k" is already handled by the separate affirmative-confirmation path —
    # this fallback only trusts a BARE number, not a sentence containing one.
    message = "Get me more WhatsApp messages. yes 10k"
    assert _extract_trailing_bare_budget(message, BUDGET_QUESTION) is None
