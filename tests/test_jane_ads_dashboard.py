"""
The Home surface (DASH-PRD-01 §4) and the conversation-measurability rule it rests on.

The rule these tests exist for: Meta reports a conversation count ONLY for native
Click-to-WhatsApp ads, so a wa.me fallback campaign reads 0 for its whole life. That
zero means "we cannot see this", not "nobody messaged you" — and at the time of
writing 20 of 21 real campaigns were fallbacks, so printing the raw metric would have
told almost every client their ads produced nothing.
"""
from datetime import datetime, timezone

from app.agents.jane_ads import constants as C
from app.agents.jane_ads import dashboard as D
from app.agents.jane_ads import measurability as M


def _row(**kw):
    base = dict(
        campaign_id="c1", name="bags for students", status="active",
        destination_type="whatsapp", budget_ngn=25_000.0,
        conversations_reportable=True, conversations_state=M.MEASURABLE,
        metrics={"spend_ngn": 14_200.0, "conversations": 18, "ends_at": None},
    )
    base.update(kw)
    return base


# ── measurability ────────────────────────────────────────────────────────────

def test_a_native_campaign_may_report_conversations():
    assert M.conversations_reportable({"conversations_measurable": True}) is True
    assert M.conversation_count({"conversations_measurable": True}, 18) == 18


def test_a_wa_me_campaign_is_suppressed_not_zeroed():
    """None, never 0 — the caller must say "we can't count these", which is the
    opposite message to "nobody messaged you"."""
    rec = {"conversations_measurable": False}
    assert M.conversations_reportable(rec) is False
    assert M.conversation_count(rec, 0) is None
    assert M.conversation_count(rec, 7) is None


def test_a_record_predating_the_stamp_is_unknown_not_false():
    """Most old records were fallbacks, but some were native. Asserting either way
    about a real client's campaign on a guess is what this module exists to prevent."""
    assert M.conversation_state({}) == M.UNKNOWN
    assert M.conversations_reportable({}) is False
    assert M.conversation_count({}, 3) is None


def test_the_suppression_reason_avoids_platform_vocabulary():
    """§9's translation rule: if it can't be said in a sentence a market vendor would
    use, it doesn't appear."""
    reason = M.unreportable_reason({"conversations_measurable": False})
    assert reason
    for jargon in ("native", "CTWA", "onsite_conversion", "messaging_conversation",
                   "optimization_goal", "promoted_object", "API"):
        assert jargon.lower() not in reason.lower()


# ── §4.1 since you last looked ───────────────────────────────────────────────

def test_the_delta_counts_only_measurable_campaigns():
    rows = [
        _row(campaign_id="a", metrics={"spend_ngn": 1.0, "conversations": 12}),
        _row(campaign_id="b", metrics={"spend_ngn": 1.0, "conversations": 6}),
    ]
    out = D.since_you_last_looked(rows, datetime(2026, 9, 9, tzinfo=timezone.utc))
    assert out["people_messaged"] == 18
    assert out["countable_campaigns"] == 2
    assert out["uncountable_campaigns"] == 0


def test_unmeasurable_campaigns_are_surfaced_not_folded_into_the_headline():
    """A fallback campaign must not quietly depress the headline — it is reported
    separately so the client learns the number is incomplete."""
    rows = [
        _row(campaign_id="a", metrics={"spend_ngn": 1.0, "conversations": 12}),
        _row(campaign_id="b", conversations_reportable=False,
             metrics={"spend_ngn": 1.0, "conversations": 0}),
    ]
    out = D.since_you_last_looked(rows, None)
    assert out["people_messaged"] == 12       # not 12 + a misleading 0
    assert out["uncountable_campaigns"] == 1


def test_no_measurable_campaign_reports_none_rather_than_zero():
    rows = [_row(conversations_reportable=False, metrics={"spend_ngn": 1.0, "conversations": 0})]
    out = D.since_you_last_looked(rows, None)
    assert out["people_messaged"] is None
    assert out["uncountable_campaigns"] == 1


def test_a_first_visit_has_no_since():
    out = D.since_you_last_looked([_row()], None)
    assert out["since"] is None


# ── §4.2 live strip ──────────────────────────────────────────────────────────

def test_only_running_campaigns_appear_in_the_strip():
    rows = [_row(campaign_id="live"), _row(campaign_id="done", status="completed"),
            _row(campaign_id="off", status="paused")]
    assert [c["campaign_id"] for c in D.live_campaign_strip(rows)] == ["live"]


def test_a_campaign_in_review_still_counts_as_live():
    """It is approved-and-spending imminently, and the client has already committed
    the money — hiding it until approval reads as the launch having failed."""
    assert len(D.live_campaign_strip([_row(status="in review")])) == 1


def test_the_strip_shows_the_stated_budget_not_the_ad_spend():
    [card] = D.live_campaign_strip([_row(budget_ngn=25_000.0)])
    assert card["budget_ngn"] == 25_000.0
    assert card["spent_ngn"] == 14_200.0


def test_the_strip_omits_a_count_it_cannot_stand_behind():
    [card] = D.live_campaign_strip([
        _row(conversations_reportable=False, metrics={"spend_ngn": 1.0, "conversations": None})])
    assert card["people_messaged"] is None
    assert card["conversations_reportable"] is False


# ── §4.4 money ───────────────────────────────────────────────────────────────

def test_the_wallet_turns_amber_below_one_campaigns_worth():
    assert D.money_line(C.MIN_TOPUP_NGN - 1, 12)["low"] is True
    assert D.money_line(C.MIN_TOPUP_NGN, 12)["low"] is False


# ── §4.3 suggestions ─────────────────────────────────────────────────────────

def test_nothing_worth_saying_produces_an_empty_list():
    """§4.3 is explicit: an empty suggestion block is more trustworthy than a padded
    one. No filler, ever."""
    money = D.money_line(50_000, 12)
    assert D.build_suggestions([_row()], money) == []


def test_an_unmeasurable_live_campaign_is_the_first_thing_raised():
    money = D.money_line(50_000, 12)
    out = D.build_suggestions(
        [_row(conversations_reportable=False, conversations_state=M.UNMEASURABLE,
              metrics={"spend_ngn": 5_000.0, "conversations": None})],
        money)
    assert out[0]["kind"] == "link_whatsapp_number"
    assert out[0]["action"] == "connections"


def test_only_one_link_suggestion_however_many_campaigns_qualify():
    """The three-cap is load-bearing — five unmeasurable campaigns must not fill the
    whole block with the same advice."""
    rows = [_row(campaign_id=f"c{i}", conversations_reportable=False,
                 conversations_state=M.UNMEASURABLE,
                 metrics={"spend_ngn": 5_000.0, "conversations": None}) for i in range(5)]
    out = D.build_suggestions(rows, D.money_line(50_000, 12))
    assert len([s for s in out if s["kind"] == "link_whatsapp_number"]) == 1


def test_a_quiet_campaign_is_only_called_quiet_when_the_count_is_real():
    """An unmeasurable campaign has no evidence either way and must never be
    reported as underperforming — that would be the suppressed zero leaking back in
    through the advice."""
    unmeasurable = _row(conversations_reportable=False,
                        metrics={"spend_ngn": 9_000.0, "conversations": None})
    out = D.build_suggestions([unmeasurable], D.money_line(50_000, 12))
    assert not any(s["kind"] == "campaign_quiet" for s in out)

    measurable = _row(metrics={"spend_ngn": 9_000.0, "conversations": 0})
    out = D.build_suggestions([measurable], D.money_line(50_000, 12))
    assert any(s["kind"] == "campaign_quiet" for s in out)


def test_a_spending_campaign_with_messages_is_not_flagged():
    out = D.build_suggestions([_row(metrics={"spend_ngn": 9_000.0, "conversations": 4})],
                              D.money_line(50_000, 12))
    assert not any(s["kind"] == "campaign_quiet" for s in out)


def test_a_low_wallet_is_raised_last():
    """It blocks the NEXT campaign rather than damaging a running one."""
    rows = [_row(conversations_reportable=False, conversations_state=M.UNMEASURABLE,
                 metrics={"spend_ngn": 5_000.0, "conversations": None})]
    out = D.build_suggestions(rows, D.money_line(100, 12))
    assert out[-1]["kind"] == "wallet_low"


def test_never_more_than_three_suggestions():
    rows = [_row(campaign_id="a", conversations_reportable=False,
                 conversations_state=M.UNMEASURABLE,
                 metrics={"spend_ngn": 5_000.0, "conversations": None})]
    rows += [_row(campaign_id=f"q{i}", metrics={"spend_ngn": 9_000.0, "conversations": 0})
             for i in range(6)]
    out = D.build_suggestions(rows, D.money_line(100, 12))
    assert len(out) <= D.MAX_SUGGESTIONS == 3


def test_an_old_campaign_is_never_told_to_go_link_a_number():
    """UNKNOWN is not UNMEASURABLE. A campaign that merely predates the stamp may
    already be native — live case: one launched hours before the stamp existed — and
    telling its owner to fix something that isn't broken is advice we cannot stand
    behind."""
    rows = [_row(conversations_reportable=False, conversations_state=M.UNKNOWN,
                 metrics={"spend_ngn": 5_000.0, "conversations": None})]
    out = D.build_suggestions(rows, D.money_line(50_000, 12))
    assert not any(s["kind"] == "link_whatsapp_number" for s in out)
