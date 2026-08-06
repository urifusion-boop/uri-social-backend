"""
Unit test for _last_known_understood (router.py) — the fix for a live-confirmed
regression: after the user picked an audience variant then answered a follow-up
location question, budget_ngn (established two turns earlier as 10000) came back
None on the very next re-parse, and the system re-asked for it as if it had never
been given. Root cause: build_history_turns only turns a prior "jane" result into a
visible turn when it carries a `question` field or is stage planned/launched — a
choose_plan_variant result (no `question` field) is invisible to the re-parse, so
whatever it established silently vanishes on the next turn. _last_known_understood
folds every prior result's `understood` into one running snapshot so an established
fact never reverts to blank.
"""
from app.agents.jane_ads.router import _last_known_understood


def _result_msg(understood: dict) -> dict:
    return {"role": "jane", "kind": "result", "result": {"understood": understood}}


def test_backfills_budget_dropped_by_a_later_reparse():
    # Reproduces the real thread verbatim: budget_ngn=10000/city="Lagos" established,
    # then a choose_plan_variant turn (no `question`) with blank understood, then a
    # need_more turn that also came back blank.
    saved = [
        {"role": "user", "kind": "text", "text": "Get me more WhatsApp messages"},
        _result_msg({"budget_ngn": None, "city": ""}),
        {"role": "user", "kind": "text", "text": "yes 10k"},
        _result_msg({"budget_ngn": 10000, "city": "Lagos", "geo_mode": "non_local",
                      "goal": "messages", "offer_type": "service"}),
        _result_msg({"budget_ngn": None, "city": ""}),
    ]
    merged = _last_known_understood(saved)
    assert merged["budget_ngn"] == 10000
    assert merged["city"] == "Lagos"
    assert merged["geo_mode"] == "non_local"
    assert merged["goal"] == "messages"
    assert merged["offer_type"] == "service"


def test_most_recent_non_empty_value_wins():
    saved = [
        _result_msg({"city": "Lagos"}),
        _result_msg({"city": "Ikeja"}),
        _result_msg({"city": ""}),
    ]
    assert _last_known_understood(saved)["city"] == "Ikeja"


def test_ignores_user_and_text_messages():
    saved = [
        {"role": "user", "kind": "text", "text": "10000"},
        {"role": "jane", "kind": "text", "text": "Got it."},
    ]
    assert _last_known_understood(saved) == {}


def test_empty_history_yields_empty_snapshot():
    assert _last_known_understood([]) == {}
