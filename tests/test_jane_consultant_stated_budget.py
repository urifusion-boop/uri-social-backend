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


# Live-reported: "Uri Social. 30,000", then "generating leads, lekki" — and Jane asked
# whether to spend last campaign's ₦130,000 instead.
import asyncio
import json
from types import SimpleNamespace

from app.agents.jane_ads import jane_consultant as jc


def test_a_bare_comma_figure_is_a_budget_but_a_count_is_not():
    assert jc.stated_budget_ngn("Uri Social. 30,000") == 30000
    assert jc.stated_budget_ngn("1,000 customers") is None


def test_the_budget_from_an_earlier_reply_is_still_the_budget():
    history = [{"role": "assistant", "content": "Last time you spent ₦130,000"}]
    assert jc.client_stated_budget("Uri Social. 30,000. generating leads, lekki", history) == 30000


def _model_says(monkeypatch, data, seen):
    class _Completions:
        async def create(self, **kw):
            seen.append(kw["messages"][-1]["content"])
            msg = SimpleNamespace(content=json.dumps(data))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    class _Client:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(jc.openai, "AsyncOpenAI", _Client)
    monkeypatch.setattr(jc, "settings", SimpleNamespace(jane_ads_openai_key="test"))


def test_a_stated_budget_is_never_asked_about_again(monkeypatch):
    seen: list[str] = []
    _model_says(monkeypatch, {
        "stage": "ask",
        "clarify": ("Given your budget is ₦30,000, I'd suggest areas within Lekki. From your "
                    "previous campaign you mentioned spending ₦130,000; would you like to "
                    "allocate a similar amount?"),
    }, seen)
    brief = asyncio.run(jc.consult("Uri Social. 30,000. generating leads, lekki",
                                   "Uri Social", "Marketing", known_budget=130000,
                                   history=[], objective_label="Leads"))
    assert brief.budget_ngn == 30000
    assert "130,000" not in (brief.clarify or "")
    assert "130,000" not in seen[0]
    assert "objective: Leads" in seen[0]


# Live-reproduced: a multi-platform campaign ("Run on multiple platforms", 3 platforms
# picked) sends the SAME brief to all 3 with an explicit per-platform budget_ngn_override
# — never typed into the message, since the frontend computed the split itself. Before
# router.py threaded this into consult() as budget_override, the model had no way to see
# it (client_stated_budget only reads message/history) and asked all 3 platforms for a
# budget anyway, identically, even though one had already been decided and sent as a
# real number — the clarifying answer to "what's your budget?" every single time.
def test_a_multi_platform_budget_override_is_never_asked_about_either(monkeypatch):
    seen: list[str] = []
    _model_says(monkeypatch, {
        "stage": "ask",
        "clarify": "Given your budget is ₦30,000, which areas of Lagos should I target?",
    }, seen)
    brief = asyncio.run(jc.consult(
        "Promote our Jane AI marketing assistant to small business owners in Lagos.",
        "Uri Social", "Marketing", known_budget=5600, history=[],
        objective_label="Leads", budget_override=30000,
    ))
    assert brief.budget_ngn == 30000
    assert "5,600" not in (brief.clarify or "")
    assert "5,600" not in seen[0]
    assert "decided" in seen[0] and "30000" in seen[0]
