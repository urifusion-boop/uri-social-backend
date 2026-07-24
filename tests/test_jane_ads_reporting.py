"""
Unit tests for the admin billing rollup (reporting.py) — per-customer real ad spend,
what we billed, and margin. Pure aggregation, no DB.
"""
from app.agents.jane_ads.reporting import summarize_billing, to_csv


def _charge(business_id, spend, billed, campaign_id="c1"):
    # Charges are stored with a NEGATIVE amount_ngn (a debit); `actual_platform_cost_ngn`
    # is the raw Meta spend the charge covered.
    return {"type": "ad_spend", "business_id": business_id, "amount_ngn": -billed,
            "actual_platform_cost_ngn": spend, "campaign_id": campaign_id}


def test_rolls_up_per_user_with_margin():
    txns = [
        _charge("brnd_A", spend=1000, billed=1500, campaign_id="c1"),
        _charge("brnd_A", spend=500, billed=750, campaign_id="c2"),
        _charge("brnd_B", spend=2000, billed=3000, campaign_id="c3"),
    ]
    s = summarize_billing(txns)
    a = next(r for r in s["per_user"] if r["business_id"] == "brnd_A")
    b = next(r for r in s["per_user"] if r["business_id"] == "brnd_B")

    assert a["real_spend_ngn"] == 1500 and a["billed_ngn"] == 2250 and a["margin_ngn"] == 750
    assert a["charges"] == 2 and a["campaigns"] == 2
    assert b["real_spend_ngn"] == 2000 and b["billed_ngn"] == 3000 and b["margin_ngn"] == 1000
    assert b["campaigns"] == 1

    assert s["totals"] == {
        "real_spend_ngn": 3500, "billed_ngn": 5250, "margin_ngn": 1750,
        "charges": 3, "users": 2,
    }


def test_sorted_by_billed_desc():
    txns = [_charge("small", 100, 150), _charge("big", 9000, 13500), _charge("mid", 1000, 1500)]
    s = summarize_billing(txns)
    assert [r["business_id"] for r in s["per_user"]] == ["big", "mid", "small"]


def test_ignores_non_ad_spend_rows():
    txns = [
        {"type": "topup", "business_id": "brnd_A", "amount_ngn": 10000},
        {"type": "conversation_charge", "business_id": "brnd_A", "amount_ngn": -400,
         "actual_platform_cost_ngn": 300, "campaign_id": "c1"},
        _charge("brnd_A", spend=1000, billed=1500),
    ]
    s = summarize_billing(txns)
    assert s["totals"]["users"] == 1
    assert s["totals"]["real_spend_ngn"] == 1000 and s["totals"]["billed_ngn"] == 1500


def test_refund_reduces_billed_and_margin_not_real_spend():
    txns = [
        _charge("brnd_A", spend=1000, billed=1500),           # billed 1500, real 1000
        {"type": "refund", "business_id": "brnd_A", "amount_ngn": 500},   # credit back 500
    ]
    s = summarize_billing(txns)
    a = s["per_user"][0]
    assert a["real_spend_ngn"] == 1000        # Meta still cost us this
    assert a["billed_ngn"] == 1000            # 1500 billed − 500 refunded
    assert a["margin_ngn"] == 0               # 1000 net billed − 1000 real spend
    assert s["totals"]["billed_ngn"] == 1000 and s["totals"]["margin_ngn"] == 0


def test_empty_ledger():
    s = summarize_billing([])
    assert s["per_user"] == []
    assert s["totals"] == {"real_spend_ngn": 0.0, "billed_ngn": 0.0, "margin_ngn": 0.0,
                           "charges": 0, "users": 0}


def test_csv_has_row_per_user_plus_total():
    s = summarize_billing([_charge("brnd_A", 1000, 1500), _charge("brnd_B", 2000, 3000)])
    s["per_user"][0]["label"] = "a@x.com"
    csv = to_csv(s)
    lines = [l for l in csv.strip().split("\n") if l]
    assert lines[0].startswith("business_id,label,campaigns,charges,real_ad_spend_ngn,billed_ngn,margin_ngn")
    assert len(lines) == 1 + 2 + 1                 # header + 2 users + TOTAL
    assert lines[-1].startswith("TOTAL,2 customers")
