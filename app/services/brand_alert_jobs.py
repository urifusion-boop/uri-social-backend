"""
Scheduled Brand Playbook alerts: weekly report, trending topic, calendar
refill, engagement spike.

Each job looks at brands that switched that alert on in the Playbook and
sends it through brand_notifier (their chosen channel + the in-app panel).
Nothing is sent to brands that didn't ask for it, and each alert is
de-duplicated so the same thing is never reported twice.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.services import brand_notifier

SPIKE_MIN_IMPRESSIONS = 100   # ignore tiny posts — 3 likes on 10 views isn't a spike
SPIKE_MIN_HISTORY = 5         # need a baseline before calling anything unusual
SPIKE_FACTOR = 2.0            # at least double the usual engagement rate
REFILL_DAYS_BEFORE_END = 5
TREND_MIN_GROWTH = 50.0       # % growth on Google Trends


def _app_link(path: str = "/workspace") -> str:
    base = (settings.WEB_APP_URL or "https://www.urisocial.com").strip("'\"").rstrip("/")
    return f"{base}{path}"


async def _brands_with(db: AsyncIOMotorDatabase, event: str) -> List[Dict[str, Any]]:
    label = brand_notifier.EVENT_LABELS[event]
    return await db["brand_profiles"].find({"notification_events": label}).to_list(length=None)


def _ids(profile: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    return profile.get("user_id") or "", profile.get("brand_id")


def _draft_scope(user_id: str, brand_id: Optional[str]) -> Dict[str, Any]:
    """Same brand isolation as PerformanceAnalyticsService."""
    from app.models.brand_account import BrandAccount

    if brand_id and brand_id != BrandAccount.personal_brand_id(user_id):
        return {"brand_id": brand_id}
    return {"user_id": user_id}


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


async def _published_with_stats(db, user_id: str, brand_id: Optional[str], since: datetime) -> List[Dict[str, Any]]:
    drafts = await db["content_drafts"].find(
        {**_draft_scope(user_id, brand_id), "status": "published"},
        {"id": 1, "platform": 1, "content": 1, "published_date": 1, "updated_at": 1},
    ).sort("updated_at", -1).to_list(length=300)
    out = []
    for d in drafts:
        when = _as_datetime(d.get("published_date")) or _as_datetime(d.get("updated_at"))
        if when and when >= since:
            out.append({**d, "_published": when})
    if not out:
        return []
    stats = await db["content_analytics"].find(
        {"draft_id": {"$in": [d["id"] for d in out if d.get("id")]}},
        {"draft_id": 1, "likes": 1, "comments": 1, "shares": 1, "impressions": 1},
    ).to_list(length=300)
    by_id = {s["draft_id"]: s for s in stats}
    for d in out:
        s = by_id.get(d.get("id"), {})
        d["likes"] = float(s.get("likes") or 0)
        d["comments"] = float(s.get("comments") or 0)
        d["shares"] = float(s.get("shares") or 0)
        d["impressions"] = float(s.get("impressions") or 0)
        interactions = d["likes"] + d["comments"] + d["shares"]
        d["engagement_rate"] = (interactions / d["impressions"] * 100) if d["impressions"] > 0 else 0.0
    return out


# ── Weekly performance report ────────────────────────────────────────────────

def compose_weekly_report(this_week: List[Dict[str, Any]], last_week: List[Dict[str, Any]]) -> Tuple[str, str]:
    def totals(posts):
        return (len(posts), sum(p["impressions"] for p in posts),
                sum(p["likes"] + p["comments"] + p["shares"] for p in posts))

    n, impressions, interactions = totals(this_week)
    pn, p_impressions, p_interactions = totals(last_week)
    if n == 0:
        return "Your weekly report", "No posts went out in the last 7 days. Jane can line some up — open URI Social."

    def change(now, before):
        if before <= 0:
            return ""
        pct = round((now - before) / before * 100)
        return f" ({'+' if pct >= 0 else ''}{pct}% vs last week)"

    lines = [
        f"Posts published: {n}" + (f" (last week: {pn})" if pn else ""),
        f"Views: {int(impressions):,}{change(impressions, p_impressions)}",
        f"Likes, comments & shares: {int(interactions):,}{change(interactions, p_interactions)}",
    ]
    best = max(this_week, key=lambda p: (p["engagement_rate"], p["impressions"]))
    if best["impressions"] > 0:
        preview = (best.get("content") or "").strip().replace("\n", " ")[:90]
        lines.append(f"Best post ({best.get('platform', '')}): \"{preview}\" — {best['engagement_rate']:.1f}% engagement")
    return "Your weekly performance report", "\n".join(lines)


async def run_weekly_reports(db: AsyncIOMotorDatabase) -> Dict[str, int]:
    now = datetime.utcnow()
    sent = 0
    week_key = now.strftime("%G-W%V")
    for profile in await _brands_with(db, "weekly_report"):
        user_id, brand_id = _ids(profile)
        if not user_id:
            continue
        try:
            recent = await _published_with_stats(db, user_id, brand_id, now - timedelta(days=14))
            this_week = [p for p in recent if p["_published"] >= now - timedelta(days=7)]
            last_week = [p for p in recent if p["_published"] < now - timedelta(days=7)]
            title, message = compose_weekly_report(this_week, last_week)
            res = await brand_notifier.notify(
                db, user_id, brand_id, "weekly_report", title, message, link=_app_link(),
                dedupe_key=week_key, dedupe_hours=24 * 6, profile=profile, only_if_configured=True,
            )
            sent += 0 if res.get("skipped") else 1
        except Exception as exc:
            print(f"[Alerts] weekly report failed for user={user_id} brand={brand_id}: {exc}")
    return {"weekly_reports_sent": sent}


# ── Engagement spike ─────────────────────────────────────────────────────────

def find_spikes(recent: List[Dict[str, Any]], baseline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    history = [p["engagement_rate"] for p in baseline if p["impressions"] > 0]
    if len(history) < SPIKE_MIN_HISTORY:
        return []
    usual = sum(history) / len(history)
    return [
        p for p in recent
        if p["impressions"] >= SPIKE_MIN_IMPRESSIONS and p["engagement_rate"] >= max(usual * SPIKE_FACTOR, usual + 1.0)
    ]


async def run_engagement_spikes(db: AsyncIOMotorDatabase) -> Dict[str, int]:
    now = datetime.utcnow()
    sent = 0
    for profile in await _brands_with(db, "engagement_spike"):
        user_id, brand_id = _ids(profile)
        if not user_id:
            continue
        try:
            posts = await _published_with_stats(db, user_id, brand_id, now - timedelta(days=90))
            recent = [p for p in posts if p["_published"] >= now - timedelta(days=3)]
            baseline = [p for p in posts if p["_published"] < now - timedelta(days=3)]
            usual = (sum(p["engagement_rate"] for p in baseline if p["impressions"] > 0)
                     / max(1, len([p for p in baseline if p["impressions"] > 0])))
            for post in find_spikes(recent, baseline):
                preview = (post.get("content") or "").strip().replace("\n", " ")[:90]
                res = await brand_notifier.notify(
                    db, user_id, brand_id, "engagement_spike",
                    title=f"Your {post.get('platform', '')} post is taking off",
                    message=(f"\"{preview}\" is at {post['engagement_rate']:.1f}% engagement — "
                             f"about {post['engagement_rate'] / usual:.0f}x your usual {usual:.1f}%. "
                             f"A good moment to reply to comments or boost it."),
                    link=_app_link(), dedupe_key=post.get("id"), dedupe_hours=24 * 7,
                    profile=profile, only_if_configured=True,
                )
                sent += 0 if res.get("skipped") else 1
        except Exception as exc:
            print(f"[Alerts] engagement spike check failed for user={user_id} brand={brand_id}: {exc}")
    return {"engagement_spikes_sent": sent}


# ── Content calendar needs refill ────────────────────────────────────────────

async def run_calendar_refill(db: AsyncIOMotorDatabase) -> Dict[str, int]:
    from app.agents.content_calendar_v2.services import content_calendar_v2_service as cal

    today = datetime.utcnow().date()
    sent = 0
    for profile in await _brands_with(db, "calendar_refill"):
        user_id, brand_id = _ids(profile)
        if not user_id:
            continue
        try:
            plan = await db[cal.COLLECTION].find_one(
                {**cal._cal_v2_scope(user_id, brand_id), "status": {"$in": ["active", "generating"]}},
                sort=[("created_at", -1)],
            )
            if not plan or plan.get("status") == "generating" or not plan.get("period_end"):
                continue  # no plan yet (nothing to refill) or a new one is on its way
            end = datetime.strptime(plan["period_end"], "%Y-%m-%d").date()
            days_left = (end - today).days
            if days_left > REFILL_DAYS_BEFORE_END:
                continue
            message = (f"Your 30-day content plan ends in {days_left} day{'s' if days_left != 1 else ''}. "
                       if days_left >= 0 else "Your 30-day content plan has ended. ")
            res = await brand_notifier.notify(
                db, user_id, brand_id, "calendar_refill",
                title="Time to plan your next month",
                message=message + "Generate the next plan so Jane keeps your posts coming.",
                link=_app_link(), dedupe_key=plan.get("plan_id"), dedupe_hours=24 * 30,
                profile=profile, only_if_configured=True,
            )
            sent += 0 if res.get("skipped") else 1
        except Exception as exc:
            print(f"[Alerts] calendar refill check failed for user={user_id} brand={brand_id}: {exc}")
    return {"calendar_refills_sent": sent}


# ── Trending topic in your niche ─────────────────────────────────────────────

def pick_trend(keywords: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The strongest REAL Google Trends riser. Never the placeholder seed
    keywords the trends service falls back to when Google is unavailable."""
    real = [k for k in keywords if k.get("source") == "google_trends"
            and float(k.get("growth_rate") or 0) >= TREND_MIN_GROWTH]
    return max(real, key=lambda k: float(k.get("growth_rate") or 0)) if real else None


async def run_trending_topics(db: AsyncIOMotorDatabase) -> Dict[str, int]:
    from app.services.TrendDataService import TrendDataService

    sent = 0
    for profile in await _brands_with(db, "trending_topic"):
        user_id, brand_id = _ids(profile)
        if not user_id or not profile.get("industry"):
            continue
        try:
            seeds = list(profile.get("content_pillars") or []) + list(profile.get("key_products_services") or [])
            keywords = await TrendDataService.get_trending_keywords(
                industry=profile["industry"], region=profile.get("region") or "", brand_seeds=seeds, db=db,
            )
            trend = pick_trend(keywords)
            if not trend:
                continue
            res = await brand_notifier.notify(
                db, user_id, brand_id, "trending_topic",
                title=f"Trending now: {trend['keyword']}",
                message=(f"Searches for \"{trend['keyword']}\" are up about {float(trend['growth_rate']):.0f}% "
                         f"(Google Trends). Want Jane to make a post about it?"),
                link=_app_link(), dedupe_key=str(trend["keyword"]).lower(), dedupe_hours=24 * 7,
                profile=profile, only_if_configured=True,
            )
            sent += 0 if res.get("skipped") else 1
        except Exception as exc:
            print(f"[Alerts] trending topic check failed for user={user_id} brand={brand_id}: {exc}")
    return {"trending_topics_sent": sent}
