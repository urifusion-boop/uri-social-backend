"""
Jane + Ads — admin billing rollup (per-customer ad spend vs. what we billed).

Pure aggregation over the wallet ledger, so it's deterministic and unit-tested. The
router feeds it AD_SPEND transactions (optionally date-filtered) and it rolls them up
per business plus grand totals:
  - real_spend_ngn : what Meta actually charged us (the `actual_platform_cost_ngn`
    recorded on each charge)
  - billed_ngn     : what we charged the customer's wallet (real spend × markup)
  - margin_ngn     : billed − real spend (our service fee earned)
"""
from __future__ import annotations

from typing import Iterable

AD_SPEND = "ad_spend"


def summarize_billing(transactions: Iterable[dict]) -> dict:
    """Roll up AD_SPEND ledger entries. Each txn dict is expected to carry:
    `type`, `business_id`, `amount_ngn` (negative — the charge), `campaign_id`,
    `actual_platform_cost_ngn` (the Meta spend that charge covered). Non-AD_SPEND
    rows are ignored, so the caller can pass a mixed ledger safely.

    Returns {"per_user": [...sorted by billed desc...], "totals": {...}}."""
    per_user: dict[str, dict] = {}
    for t in transactions:
        if t.get("type") != AD_SPEND:
            continue
        bid = t.get("business_id") or "unknown"
        row = per_user.setdefault(bid, {
            "business_id": bid, "real_spend_ngn": 0.0, "billed_ngn": 0.0,
            "margin_ngn": 0.0, "charges": 0, "_campaigns": set(),
        })
        real = float(t.get("actual_platform_cost_ngn") or 0.0)
        billed = abs(float(t.get("amount_ngn") or 0.0))   # charges are stored negative
        row["real_spend_ngn"] += real
        row["billed_ngn"] += billed
        row["charges"] += 1
        if t.get("campaign_id"):
            row["_campaigns"].add(t["campaign_id"])

    rows = []
    totals = {"real_spend_ngn": 0.0, "billed_ngn": 0.0, "margin_ngn": 0.0,
              "charges": 0, "users": 0}
    for row in per_user.values():
        row["real_spend_ngn"] = round(row["real_spend_ngn"], 2)
        row["billed_ngn"] = round(row["billed_ngn"], 2)
        row["margin_ngn"] = round(row["billed_ngn"] - row["real_spend_ngn"], 2)
        row["campaigns"] = len(row.pop("_campaigns"))
        rows.append(row)
        totals["real_spend_ngn"] += row["real_spend_ngn"]
        totals["billed_ngn"] += row["billed_ngn"]
        totals["margin_ngn"] += row["margin_ngn"]
        totals["charges"] += row["charges"]

    totals = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in totals.items()}
    totals["users"] = len(rows)
    rows.sort(key=lambda r: r["billed_ngn"], reverse=True)
    return {"per_user": rows, "totals": totals}


def to_csv(summary: dict) -> str:
    """Render the rollup as CSV (one row per customer, plus a TOTAL row) — openable
    in any spreadsheet."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["business_id", "label", "campaigns", "charges",
                "real_ad_spend_ngn", "billed_ngn", "margin_ngn"])
    for r in summary["per_user"]:
        w.writerow([r["business_id"], r.get("label", ""), r["campaigns"], r["charges"],
                    r["real_spend_ngn"], r["billed_ngn"], r["margin_ngn"]])
    t = summary["totals"]
    w.writerow(["TOTAL", f"{t['users']} customers", "", t["charges"],
                t["real_spend_ngn"], t["billed_ngn"], t["margin_ngn"]])
    return buf.getvalue()
