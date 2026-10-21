"""
Meta insights → daily snapshots (PRD §4.2, §7).

One row per ad per day, upserted, so a re-pull corrects Meta's late attribution
instead of double-counting. Every row carries where it came from, the reporting
window and attribution setting, and when it was retrieved. A failed pull is
recorded as connector health — never as a day of zeros.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

SNAPSHOTS = "jane_ads_metric_snapshots"
HEALTH = "jane_ads_growth_health"

SOURCE = "meta_insights"
ATTRIBUTION = "Meta default (7-day click, 1-day view)"
LOOKBACK_DAYS = 28


def _conversations(actions: list) -> int:
    from ..adapters.meta import _CONVERSATION_ACTION_TYPE
    for a in actions or []:
        if a.get("action_type") == _CONVERSATION_ACTION_TYPE:
            return int(float(a.get("value") or 0))
    return 0


def parse_rows(campaign_id: str, data: list[dict], retrieved_at: datetime) -> list[dict]:
    rows = []
    for d in data:
        rows.append({
            "campaign_id": campaign_id,
            "adset_id": str(d.get("adset_id") or ""),
            "ad_id": str(d.get("ad_id") or ""),
            "ad_name": d.get("ad_name") or "",
            "date": d.get("date_start"),
            "spend_ngn": float(d.get("spend") or 0),
            "impressions": int(d.get("impressions") or 0),
            "reach": int(d.get("reach") or 0),
            "clicks": int(d.get("clicks") or 0),
            "conversations": _conversations(d.get("actions")),
            "currency": d.get("account_currency") or "NGN",
            "source": SOURCE,
            "reporting_window": "1d",
            "attribution_window": ATTRIBUTION,
            "retrieved_at": retrieved_at,
        })
    return rows


_STATUSES = ["ACTIVE", "PAUSED", "DELETED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES",
             "CAMPAIGN_PAUSED", "ADSET_PAUSED", "PENDING_REVIEW", "DISAPPROVED", "PREAPPROVED",
             "PENDING_BILLING_INFO"]


async def _fetch(client, adapter, campaign_id: str, params: dict) -> list[dict]:
    out: list[dict] = []
    url: Optional[str] = f"{adapter._graph_base}/{campaign_id}/insights"
    p: Optional[dict] = {"access_token": adapter._access_token, **params}
    while url:
        resp = await client.get(url, params=p)
        body = resp.json()
        if "error" in body:
            raise RuntimeError(body["error"].get("message") or "insights error")
        out.extend(body.get("data") or [])
        url = (body.get("paging") or {}).get("next")
        p = None      # the next link carries every parameter
    return out


async def pull(db, adapter, record: dict, today: Optional[date] = None) -> dict:
    """Refresh this campaign's snapshots. Returns {"ok", "rows", "error"}.

    Two reads, because they answer different questions. Campaign-level daily rows
    are the campaign's real totals — ad-level rows leave out ads Meta has archived
    or deleted, and a relaunched Jane campaign can have delivered through exactly
    those, which read as a day of zero spend beside real conversations. Ad-level
    rows, asked for in every status, exist only to compare ads with each other."""
    campaign_id = record["campaign_id"]
    today = today or datetime.now(timezone.utc).date()
    created = record.get("created_at")
    since = today - timedelta(days=LOOKBACK_DAYS)
    if isinstance(created, datetime):
        since = max(since, created.date())
    window = json.dumps({"since": since.isoformat(), "until": today.isoformat()})
    base = {"time_increment": 1, "time_range": window, "limit": 500,
            "fields": "date_start,spend,impressions,reach,clicks,actions,account_currency"}
    now = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            camp = await _fetch(client, adapter, campaign_id, {**base, "level": "campaign"})
            ads = await _fetch(client, adapter, campaign_id, {
                **base, "level": "ad",
                "fields": "ad_id,ad_name,adset_id," + base["fields"],
                "filtering": json.dumps([{"field": "ad.effective_status", "operator": "IN",
                                          "value": _STATUSES}])})
    except Exception as e:  # recorded, never raised: one campaign must not stop the sweep
        await db[HEALTH].update_one({"campaign_id": campaign_id}, {
            "$set": {"last_error": str(e)[:300], "last_error_at": now},
            "$inc": {"error_count": 1}}, upsert=True)
        return {"ok": False, "rows": 0, "error": str(e)[:300]}

    rows = [{**r, "level": "campaign", "ad_id": ""} for r in parse_rows(campaign_id, camp, now)] + \
        [{**r, "level": "ad"} for r in parse_rows(campaign_id, ads, now)]
    for r in rows:
        await db[SNAPSHOTS].update_one(
            {"campaign_id": campaign_id, "level": r["level"], "ad_id": r["ad_id"], "date": r["date"]},
            {"$set": {**r, "brand_id": record.get("brand_id")}}, upsert=True)
    # What Meta actually sent for one row (metrics only), so a parsing problem can be
    # seen without access to the token.
    sample = {k: v for k, v in (camp[0] if camp else {}).items() if k != "actions"}
    await db[HEALTH].update_one({"campaign_id": campaign_id}, {
        "$set": {"last_success_at": now, "last_error": "", "error_count": 0,
                 "brand_id": record.get("brand_id"), "rows_last_pull": len(camp),
                 "ad_rows_last_pull": len(ads), "sample_row": sample}},
        upsert=True)
    return {"ok": True, "rows": len(rows), "error": ""}


async def load(db, campaign_id: str, days: int = LOOKBACK_DAYS, level: str = "campaign") -> list[dict]:
    """Daily rows at one level. Campaign rows are the totals; snapshots stored before
    campaign-level reads existed have no level and are ad rows."""
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    q: dict = {"campaign_id": campaign_id, "date": {"$gte": since}}
    q["level"] = "campaign" if level == "campaign" else {"$in": ["ad", None]}
    return await db[SNAPSHOTS].find(q, {"_id": 0}).sort("date", 1).to_list(5000)


async def health(db, campaign_id: str) -> dict:
    return await db[HEALTH].find_one({"campaign_id": campaign_id}, {"_id": 0}) or {}


async def check_landing_page(url: str) -> dict:
    """Is the page a click lands on reachable and quick? Only http(s) destinations;
    a WhatsApp or Messenger link has no page to check."""
    if not url or not url.startswith(("http://", "https://")) or \
            any(h in url for h in ("wa.me", "whatsapp.com", "m.me", "instagram.com", "facebook.com")):
        return {"checked": False}
    start = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
        secs = (datetime.now(timezone.utc) - start).total_seconds()
        ok = resp.status_code < 400 and secs < 8
        return {"checked": True, "ok": ok, "status": resp.status_code, "seconds": round(secs, 1),
                "url": url, "checked_at": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        reason = {"ConnectError": "couldn't connect", "ConnectTimeout": "timed out connecting",
                  "ReadTimeout": "took too long to respond"}.get(type(e).__name__, type(e).__name__)
        return {"checked": True, "ok": False, "status": None,
                "error": f"{reason}{': ' + str(e)[:160] if str(e) else ''}", "url": url,
                "checked_at": datetime.now(timezone.utc).isoformat()}


async def destination_url(adapter, ad_id: str) -> str:
    """The website an ad sends people to, read from its creative. '' for chat ads."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{adapter._graph_base}/{ad_id}", params={
                "access_token": adapter._access_token,
                "fields": "creative{object_story_spec,link_url}"})
        cr = (resp.json().get("creative") or {})
        spec = cr.get("object_story_spec") or {}
        link = (spec.get("link_data") or {}).get("link") or cr.get("link_url") or \
            ((spec.get("video_data") or {}).get("call_to_action") or {}).get("value", {}).get("link", "")
        return link or ""
    except Exception:
        return ""
