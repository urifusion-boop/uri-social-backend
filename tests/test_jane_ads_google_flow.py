"""
Google in Jane's campaign flow: the pieces the router leans on to treat a
Google Search campaign like a Meta or TikTok one — is the brand ready, is the
budget enough, where does it target, what does the campaign list show, on/off,
delete, and the hourly spend sweep. httpx is mocked.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads import constants as C
from app.agents.jane_ads import google_flow as gf
from app.agents.jane_ads.adapters.google import GoogleAdsAdapter
from tests.test_jane_ads_google_ads_connection import FakeDb, _admin_conn_doc, _conn_doc
from tests.test_jane_ads_google_link_sync import Google


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for mod in (gf.settings,):
        monkeypatch.setattr(mod, "GOOGLE_ADS_DEVELOPER_TOKEN", "dev", raising=False)
        monkeypatch.setattr(mod, "GOOGLE_ADS_MCC_CUSTOMER_ID", "9999999999", raising=False)
        monkeypatch.setattr(mod, "GOOGLE_ADS_API_VERSION", "v24", raising=False)


def adapter(db=None, currency="USD"):
    return GoogleAdsAdapter(db or FakeDb(), customer_id="4052658060", login_customer_id="9999999999",
                            access_token="t", currency_code=currency)


RECORD = {"campaign_id": "777", "brand_id": "b1", "user_id": "u1", "business_id": "b1", "platform": "google",
          "customer_id": "4052658060", "login_customer_id": "9999999999", "currency_code": "USD",
          "ad_group_id": "customers/4052658060/adGroups/55", "ad_id": "99"}


def db_with_campaign(**extra):
    db = FakeDb()
    db["x"].docs.append({**RECORD, **extra})
    db["x"].docs.append(_admin_conn_doc())
    return db


class TestIsTheBrandReady:
    def test_linked_account_is_ready_without_any_network_call(self):
        db = FakeDb()
        db["x"].docs.append(_conn_doc(manager_link_status="active"))
        with Google() as google:
            assert run(gf.is_ready(db, "u1", "b1")) is True
        assert google.calls == []

    @pytest.mark.parametrize("conn", [dict(manager_link_status="pending"), dict(manager_link_status="none", customer_id="")])
    def test_not_ready_until_linked(self, conn):
        db = FakeDb()
        db["x"].docs.append(_conn_doc(**conn))
        assert run(gf.is_ready(db, "u1", "b1")) is False

    def test_never_ready_when_uri_itself_is_not_set_up(self, monkeypatch):
        monkeypatch.setattr(gf.settings, "GOOGLE_ADS_DEVELOPER_TOKEN", "", raising=False)
        db = FakeDb()
        db["x"].docs.append(_conn_doc(manager_link_status="active"))
        assert run(gf.is_ready(db, "u1", "b1")) is False


class TestBudgetIsEnoughForSearch:
    def test_enough(self):
        assert gf.budget_too_low(35000, 7) is None          # ₦5,000 a day ≈ 20 clicks

    def test_too_little_says_what_would_work(self):
        msg = gf.budget_too_low(7000, 7)                    # ₦1,000 a day ≈ 4 clicks
        assert "₦1,000 a day" in msg and "₦17,500" in msg and "Instagram and Facebook" in msg

    def test_going_ahead_anyway_gets_what_to_expect_not_a_refusal(self):
        msg = gf.budget_too_low_warning(5400, 5)            # ₦1,080 a day ≈ 4 clicks
        assert "₦1,080 a day" in msg and "~4 clicks" in msg and "test" in msg and "₦12,500" in msg


class TestTargetingThePlansOwnCity:
    def test_a_city_google_knows(self):
        found = {"geoTargetConstantSuggestions": [{"geoTargetConstant": {
            "resourceName": "geoTargetConstants/1010294", "status": "ENABLED"}}]}
        with Google(found) as google:
            assert run(adapter()._resolve_geo("Lagos, Nigeria")) == "geoTargetConstants/1010294"
        sent = google.calls[0]["json"]
        assert sent["countryCode"] == "NG" and sent["locationNames"]["names"] == ["Lagos"]

    @pytest.mark.parametrize("geo", ["", "Nigeria", "nationwide"])
    def test_no_place_means_the_whole_country_and_no_lookup(self, geo):
        with Google() as google:
            assert run(adapter()._resolve_geo(geo)) == "geoTargetConstants/2566"
        assert google.calls == []

    def test_unknown_place_or_a_failed_lookup_falls_back_to_the_country(self):
        with Google({"geoTargetConstantSuggestions": []}):
            assert run(adapter()._resolve_geo("Atlantis")) == "geoTargetConstants/2566"
        with Google({"error": {"message": "boom"}}):
            assert run(adapter()._resolve_geo("Lagos")) == "geoTargetConstants/2566"


class TestWhatTheCampaignListShows:
    def test_spend_in_naira_and_clicks_as_conversations(self):
        row = {"campaign": {"status": "ENABLED", "endDateTime": "2026-10-30 23:59:59"}, "customer": {"currencyCode": "USD"},
               "metrics": {"costMicros": "2000000", "impressions": "400", "clicks": "8"}}
        with Google({"results": [row]}):
            out = run(adapter().fetch_campaign_summary("777"))
        assert out["delivery"] == "Active" and out["spend_ngn"] == 2 * C.USD_TO_NGN
        assert out["conversations"] == 8 and out["cost_per_conversation_ngn"] == 2 * C.USD_TO_NGN / 8
        assert out["impressions"] == 400 and out["ends_at"] == "2026-10-30"

    def test_paused_with_no_clicks(self):
        with Google({"results": [{"campaign": {"status": "PAUSED"}, "metrics": {}}]}):
            out = run(adapter().fetch_campaign_summary("777"))
        assert out["delivery"] == "Paused" and out["cost_per_conversation_ngn"] is None and out["spend_ngn"] == 0

    def test_reads_the_end_date_field_google_still_has(self):
        # Google removed campaign.end_date in API v23 — asking for it failed the
        # whole read, and the list then showed a running campaign as paused.
        with Google({"results": [{"campaign": {"status": "PAUSED"}, "metrics": {}}]}) as google:
            run(adapter().fetch_campaign_summary("777"))
        query = google.calls[0]["json"]["query"]
        assert "campaign.end_date_time" in query and "campaign.end_date," not in query

    def test_says_when_the_account_cant_pay_for_ads(self):
        with Google({"results": [{"campaign": {"status": "ENABLED", "primaryStatus": "NOT_ELIGIBLE"}, "metrics": {}}]},
                    {"results": [{"billingSetup": {"status": "CANCELLED"}}]}):
            out = run(adapter().fetch_campaign_summary("777"))
        assert out["billing_ready"] is False
        assert "no payment method" in gf.serving_note(out)

    def test_an_approved_payment_method_counts(self):
        with Google({"results": [{"campaign": {"status": "ENABLED"}, "metrics": {}}]},
                    {"results": [{"billingSetup": {"status": "APPROVED"}}]}):
            assert run(adapter().fetch_campaign_summary("777"))["billing_ready"] is True

    @pytest.mark.parametrize("summary, says", [
        ({"delivery": "Active", "billing_ready": True, "primary_status": "PENDING",
          "primary_status_reasons": ["MOST_ADS_UNDER_REVIEW"]}, "still reviewing"),
        ({"delivery": "Active", "billing_ready": True, "primary_status": "NOT_ELIGIBLE",
          "primary_status_reasons": ["HAS_ADS_DISAPPROVED"]}, "rejected the ad"),
        ({"delivery": "Active", "billing_ready": True, "primary_status": "NOT_ELIGIBLE",
          "primary_status_reasons": []}, "isn't eligible"),
        ({"delivery": "Paused", "billing_ready": False}, "Before you turn this on"),
    ])
    def test_why_it_isnt_showing_in_plain_words(self, summary, says):
        assert says in gf.serving_note(summary)

    def test_nothing_to_say_when_it_is_serving_or_simply_paused(self):
        assert gf.serving_note({"delivery": "Active", "billing_ready": True, "primary_status": "ELIGIBLE"}) == ""
        assert gf.serving_note({"delivery": "Paused", "billing_ready": True}) == ""

    @pytest.mark.parametrize("rows", [[], [{"campaign": {"status": "REMOVED"}, "metrics": {}}]])
    def test_gone_from_google_reads_as_deleted(self, rows):
        with Google({"results": rows}):
            assert run(adapter().fetch_campaign_summary("777"))["delivery"] == "Deleted"


class TestOnOffAndDelete:
    def test_turning_on_enables_campaign_ad_group_and_ad(self):
        db = db_with_campaign()
        with Google({"results": [{}]}, {"results": [{}]}, {"results": [{}]}) as google:
            out = run(adapter(db).set_delivery("777", True))
        assert out == {"status": "ENABLED", "updated": {"campaign": True, "ad_group": True, "ad": True}}
        resources = [c["json"]["operations"][0]["update"]["resourceName"] for c in google.calls]
        assert resources == ["customers/4052658060/campaigns/777", "customers/4052658060/adGroups/55",
                             "customers/4052658060/adGroupAds/55~99"]
        assert all(c["json"]["operations"][0]["update"]["status"] == "ENABLED" for c in google.calls)

    def test_turning_off_pauses_them(self):
        db = db_with_campaign()
        with Google({"results": [{}]}, {"results": [{}]}, {"results": [{}]}) as google:
            out = run(adapter(db).set_delivery("777", False))
        assert out["status"] == "PAUSED"
        assert google.calls[0]["json"]["operations"][0]["updateMask"] == "status"

    def test_the_dispatcher_uses_each_campaigns_own_account_and_currency(self):
        db = db_with_campaign()
        row = {"campaign": {"status": "ENABLED"}, "metrics": {"costMicros": "1000000", "clicks": "1"}}
        with Google({"results": [row]}) as google:
            out = run(gf.GoogleCampaigns(db).fetch_campaign_summary("777"))
        assert "/customers/4052658060/googleAds:search" in google.calls[0]["url"]
        assert google.calls[0]["headers"]["Authorization"] == "Bearer admin_at"
        assert out["spend_ngn"] == C.USD_TO_NGN          # the record's currency, converted

    def test_only_a_real_google_campaign_of_this_brand_is_found(self):
        db = db_with_campaign()
        assert run(gf.find_record(db, "777", "b1"))["campaign_id"] == "777"
        assert run(gf.find_record(db, "777", "someone_else")) is None
        meta_lookalike = FakeDb()
        meta_lookalike["x"].docs.append({"campaign_id": "777", "brand_id": "b1"})   # no platform=google
        assert run(gf.find_record(meta_lookalike, "777", "b1")) is None


class TestHourlySpendSweep:
    """billing.py's sweep, unchanged, running over Google campaigns through the dispatcher."""

    def _sweep(self, db, summary, balance=100000.0):
        from app.agents.jane_ads import billing

        class Wallet:
            charged = []

            async def get_balance(self, business_id):
                return balance

            async def charge_ad_spend(self, business_id, amount, **kw):
                Wallet.charged.append(amount)

        class Notifier:
            async def _log_notification(self, **kw):
                pass

        coll = db["x"]

        class Cursor:
            def __init__(self, docs):
                self.docs = docs

            async def to_list(self, length=None):
                return [dict(d) for d in self.docs]

        coll.find = lambda q, proj=None: Cursor([d for d in coll.docs if d.get("platform") == "google"])

        async def find_one_and_update(query, update):
            for d in coll.docs:
                if all(d.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                    d.update(update.get("$set", {}))
                    return d
            return None

        coll.find_one_and_update = find_one_and_update
        dispatcher = gf.GoogleCampaigns(db)
        dispatcher.fetch_campaign_summary = AsyncMock(return_value=summary)
        dispatcher.set_delivery = AsyncMock(return_value={})
        Wallet.charged = []
        out = run(billing._sweep_platform(db, Wallet(), Notifier(), adapter=dispatcher,
                                          collection=gf.GOOGLE_CAMPAIGNS, api_error_cls=gf.GoogleAdsAPIError))
        return out, Wallet.charged, dispatcher

    SUMMARY = {"delivery": "Active", "spend_ngn": 3100.0, "impressions": 10, "reach": 10, "conversations": 2,
               "cost_per_conversation_ngn": 1550.0, "ends_at": None}

    def test_a_campaign_paid_for_at_launch_is_tracked_but_not_charged_again(self):
        db = db_with_campaign(charged_upfront_ngn=35000.0)
        out, charged, _ = self._sweep(db, self.SUMMARY)
        assert out["checked"] == 1 and charged == [] and out["charged_ngn"] == 0

    def test_a_campaign_removed_in_google_ads_drops_off_our_list(self):
        db = db_with_campaign()
        removed = []

        async def delete_one(query):
            removed.append(query)

        db["x"].delete_one = delete_one
        self._sweep(db, {**self.SUMMARY, "delivery": "Deleted"})
        assert removed == [{"campaign_id": "777"}]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


class TestErrorsAnOwnerCanActOn:
    @pytest.mark.parametrize("raw, says", [
        ("campaign create: The customer is not enabled. [CUSTOMER_NOT_ENABLED]", "isn't active"),
        ("GAQL search: User doesn't have permission [USER_PERMISSION_DENIED]", "Reconnect it"),
        ("ad create: A policy was violated [POLICY_FINDING]", "Ask Jane to reword"),
        ("budget create: Too many [RESOURCE_COUNT_LIMIT_EXCEEDED]", "limit on campaigns"),
    ])
    def test_googles_codes_become_plain_words(self, raw, says):
        assert says in gf.friendly_error(gf.GoogleAdsAPIError(raw))

    def test_messages_already_written_for_people_pass_through(self):
        msg = "This Google Ads account is in EUR. Budgets can only be set on NGN or USD accounts for now."
        assert gf.friendly_error(gf.GoogleAdsAPIError(msg)) == msg

    def test_anything_else_keeps_googles_reason_and_says_nothing_was_charged(self):
        out = gf.friendly_error(gf.GoogleAdsAPIError("campaign create: Some new problem [WEIRD]"))
        assert "Some new problem" in out and "Nothing was charged" in out

    @pytest.mark.parametrize("state", ["none", "needs_account_selection", "manager_link_pending",
                                       "manager_link_refused", "manager_link_declined"])
    def test_connection_problems_say_where_to_fix_them(self, state):
        assert "Connected Accounts → Google Ads" in gf.connection_message(state)


class TestPlacementMinimums:
    def test_youtube_and_discover_below_googles_floor_says_what_works(self):
        msg = gf.below_placement_minimum("demand_gen", 35000, 7)       # ₦5,000 a day
        assert "₦8,000 a day" in msg and "₦56,000" in msg and "4 days" in msg

    def test_enough_or_no_floor(self):
        assert gf.below_placement_minimum("demand_gen", 70000, 7) is None
        assert gf.below_placement_minimum("display", 7000, 7) is None
        assert gf.below_placement_minimum("search", 7000, 7) is None


class TestNeverSpendsPastWhatWasPaid:
    def test_end_date_counts_today_as_day_one(self):
        from datetime import date
        from app.agents.jane_ads.adapters.google import campaign_end
        assert campaign_end(5, date(2026, 10, 14)) == "2026-10-18 23:59:59"
        assert campaign_end(1, date(2026, 10, 14)) == "2026-10-14 23:59:59"

    def test_turning_on_sets_the_end_date_from_now(self):
        db = db_with_campaign(days=5)
        with Google({"results": [{}]}, {"results": [{}]}, {"results": [{}]}) as google:
            run(adapter(db).set_delivery("777", True))
        campaign_op = google.calls[0]["json"]["operations"][0]
        assert campaign_op["updateMask"] == "status,end_date_time"
        assert campaign_op["update"]["endDateTime"].endswith("23:59:59")

    def test_pausing_leaves_the_end_date_alone(self):
        db = db_with_campaign(days=5)
        with Google({"results": [{}]}, {"results": [{}]}, {"results": [{}]}) as google:
            run(adapter(db).set_delivery("777", False))
        assert google.calls[0]["json"]["operations"][0]["updateMask"] == "status"

    def test_the_sweep_stops_a_campaign_that_has_spent_what_was_paid(self):
        db = db_with_campaign(charged_upfront_ngn=6000.0, budget_ngn=5400.0)
        out, charged, dispatcher = TestHourlySpendSweep()._sweep(
            db, {**TestHourlySpendSweep.SUMMARY, "spend_ngn": 5450.0})
        dispatcher.set_delivery.assert_awaited_once_with("777", active=False)
        assert charged == []

    def test_still_within_budget_keeps_running(self):
        db = db_with_campaign(charged_upfront_ngn=6000.0, budget_ngn=5400.0)
        _, _, dispatcher = TestHourlySpendSweep()._sweep(db, {**TestHourlySpendSweep.SUMMARY, "spend_ngn": 3000.0})
        dispatcher.set_delivery.assert_not_awaited()
