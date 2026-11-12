"""
Analytics read straight from the direct Meta (Facebook / Instagram Graph API) and TikTok
connections, for the Performance page.

Two problems this exists to absorb:

* Meta keeps retiring insight metrics (impressions -> views, page_impressions ->
  page_media_view, plays/video_views -> views, ...). A Graph insights call that names ONE
  metric the object no longer supports fails as a whole, so the old combined requests came
  back empty and the page showed zeros. `insight_values` asks for the whole set first, and on
  an error asks for each metric on its own, so a retired name only costs that one number.
* A post URI published to TikTok directly is stored under TikTok's publish_id, not the video
  id TikTok's video list reports, so a published draft is matched to its video by time and
  caption (`match_tiktok_videos`).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable, Optional


def _num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _value_of(item: dict):
    """An insights row's number, whichever shape Meta used (total_value, or a values[] list)."""
    tv = item.get("total_value")
    if isinstance(tv, dict) and tv.get("value") is not None:
        return tv["value"]
    values = item.get("values") or []
    if values:
        return values[-1].get("value", 0)
    return 0


def _daily_of(item: dict) -> list[dict]:
    """[{"date": "2026-11-01", "value": 12}, ...] from a period=day insights row."""
    out = []
    for v in item.get("values") or []:
        end = str(v.get("end_time") or "")[:10]
        val = v.get("value")
        if end and isinstance(val, (int, float)):
            out.append({"date": end, "value": val})
    return out


async def insight_values(client, url: str, metrics: Iterable[str], params: Optional[dict] = None,
                         label: str = "") -> dict:
    """{metric_name: value} for a Graph insights edge.

    Asks for all `metrics` in one call; if Meta rejects it (one of them unsupported for this
    object / API version), asks for each on its own and keeps whichever succeed."""
    metrics = list(metrics)
    params = dict(params or {})

    async def call(names: list[str]) -> Optional[list[dict]]:
        try:
            resp = await client.get(url, params={**params, "metric": ",".join(names)})
            body = resp.json()
        except Exception as e:  # network / bad JSON
            print(f"⚠️ insights {label} {names} failed: {e}")
            return None
        if body.get("error"):
            print(f"⚠️ insights {label} {names}: {body['error'].get('message')} (code {body['error'].get('code')})")
            return None
        return body.get("data") or []

    rows = await call(metrics)
    if rows is None and len(metrics) > 1:
        singles = await asyncio.gather(*[call([m]) for m in metrics])
        rows = [r for part in singles if part for r in part]
    return {r["name"]: r for r in (rows or []) if r.get("name")}


def values_only(rows: dict) -> dict:
    return {name: _value_of(row) for name, row in rows.items()}


# ── Facebook ─────────────────────────────────────────────────────────────────

# Meta replaced the *_impressions family with "media views" (and *_unique with viewers).
# The old names are tried second so an older API version keeps working.
_FB_POST_VIEW_METRICS = ("post_media_view", "post_impressions")
_FB_POST_REACH_METRICS = ("post_total_media_view_unique", "post_impressions_unique")


async def facebook_post_insights(client, graph_base: str, post_id: str, token: str) -> dict:
    """views / reach / clicks for one Page post. Zeros (not an error) when Meta has none yet."""
    rows = values_only(await insight_values(
        client, f"{graph_base}/{post_id}/insights",
        (*_FB_POST_VIEW_METRICS, *_FB_POST_REACH_METRICS, "post_clicks"),
        {"access_token": token}, label=f"fb post {post_id}",
    ))
    views = next((_num(rows[m]) for m in _FB_POST_VIEW_METRICS if _num(rows.get(m))), 0)
    reach = next((_num(rows[m]) for m in _FB_POST_REACH_METRICS if _num(rows.get(m))), 0)
    return {"views": views, "reach": reach, "clicks": _num(rows.get("post_clicks"))}


async def facebook_page_insights(client, graph_base: str, page_id: str, token: str,
                                 since_ts: int, until_ts: int) -> dict:
    """Page totals + a daily series over the period.

    {views, reach, engagements, new_follows, daily_views: [...], daily_reach: [...]}"""
    base = {"period": "day", "since": since_ts, "until": until_ts, "access_token": token}
    rows = await insight_values(
        client, f"{graph_base}/{page_id}/insights",
        ("page_media_view", "page_total_media_view_unique", "page_post_engagements",
         "page_daily_follows_unique", "page_impressions", "page_impressions_unique"),
        base, label=f"fb page {page_id}",
    )

    def total(*names: str) -> int:
        for n in names:
            row = rows.get(n)
            if row:
                t = sum(_num(v.get("value")) for v in (row.get("values") or []))
                if t:
                    return t
        return 0

    def daily(*names: str) -> list[dict]:
        for n in names:
            row = rows.get(n)
            if row and _daily_of(row):
                return _daily_of(row)
        return []

    return {
        "views": total("page_media_view", "page_impressions"),
        "reach": total("page_total_media_view_unique", "page_impressions_unique"),
        "engagements": total("page_post_engagements"),
        "new_follows": total("page_daily_follows_unique"),
        "daily_views": daily("page_media_view", "page_impressions"),
        "daily_reach": daily("page_total_media_view_unique", "page_impressions_unique"),
    }


# ── Instagram ────────────────────────────────────────────────────────────────

# `views` is the one view counter Meta kept for every media type (feed, reel, story) after it
# retired impressions / plays / video_views; the rest are lifetime interaction counts.
IG_MEDIA_METRICS = ("views", "reach", "saved", "shares", "total_interactions")


async def instagram_media_insights(client, graph_base: str, media_id: str, token: str) -> dict:
    rows = values_only(await insight_values(
        client, f"{graph_base}/{media_id}/insights", IG_MEDIA_METRICS,
        {"period": "lifetime", "access_token": token}, label=f"ig media {media_id}",
    ))
    return {
        "views": _num(rows.get("views")),
        "reach": _num(rows.get("reach")),
        "saves": _num(rows.get("saved")),
        "shares": _num(rows.get("shares")),
        "total_interactions": _num(rows.get("total_interactions")),
    }


async def instagram_account_extras(client, graph_base: str, ig_user_id: str, token: str,
                                   since_ts: int, until_ts: int) -> dict:
    """What the account-level view gives beyond views/reach: interactions, accounts engaged,
    profile link taps, and the daily reach series for a trend line."""
    totals = values_only(await insight_values(
        client, f"{graph_base}/{ig_user_id}/insights",
        ("accounts_engaged", "total_interactions", "likes", "comments", "shares", "saves",
         "profile_links_taps"),
        {"period": "day", "metric_type": "total_value", "since": since_ts, "until": until_ts,
         "access_token": token},
        label=f"ig account totals {ig_user_id}",
    ))
    series = await insight_values(
        client, f"{graph_base}/{ig_user_id}/insights", ("reach",),
        {"period": "day", "since": since_ts, "until": until_ts, "access_token": token},
        label=f"ig account reach series {ig_user_id}",
    )
    return {
        "accounts_engaged": _num(totals.get("accounts_engaged")),
        "total_interactions": _num(totals.get("total_interactions")),
        "likes": _num(totals.get("likes")),
        "comments": _num(totals.get("comments")),
        "shares": _num(totals.get("shares")),
        "saves": _num(totals.get("saves")),
        "profile_link_taps": _num(totals.get("profile_links_taps")),
        "daily_reach": _daily_of(series["reach"]) if "reach" in series else [],
    }


# ── TikTok ───────────────────────────────────────────────────────────────────

# A draft is published seconds-to-minutes before TikTok stamps the video, and a scheduled
# post can sit in TikTok's own queue a little longer — so match generously, then let the
# closest-in-time (and caption) win.
_TT_MATCH_WINDOW_S = 6 * 3600


def _as_epoch(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                from dateutil.parser import parse as _dp
                dt = _dp(text)
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _caption_overlap(draft_text: str, video_text: str) -> bool:
    a = " ".join((draft_text or "").lower().split())[:40]
    b = " ".join((video_text or "").lower().split())
    return bool(a) and a in b


def match_tiktok_videos(drafts: list[dict], videos: list[dict]) -> dict[str, dict]:
    """{draft_id: tiktok video} — each video claimed by at most one draft.

    Candidates are videos created within the window of the draft's published_date; a caption
    that starts the same way outranks time alone, then the smaller time gap wins. Assigned
    greedily best-pair-first so two near-simultaneous drafts do not both take the same video."""
    pairs = []
    for d in drafts:
        draft_id = d.get("id")
        published = _as_epoch(d.get("published_date"))
        if not draft_id or published is None:
            continue
        for v in videos:
            created = _num(v.get("create_time"))
            if not created:
                continue
            gap = abs(created - published)
            if gap > _TT_MATCH_WINDOW_S:
                continue
            same_caption = _caption_overlap(d.get("content") or "", v.get("video_description") or v.get("title") or "")
            pairs.append((0 if same_caption else 1, gap, draft_id, v))
    pairs.sort(key=lambda x: (x[0], x[1]))
    matched: dict[str, dict] = {}
    taken: set = set()
    for _, _, draft_id, v in pairs:
        if draft_id in matched or v.get("id") in taken:
            continue
        matched[draft_id] = v
        taken.add(v.get("id"))
    return matched


def tiktok_post_metrics(video: dict) -> dict:
    """Map one TikTok video to the post-analytics shape the Performance page uses.
    TikTok reports views only, so impressions and reach are the view count."""
    views = _num(video.get("view_count"))
    likes = _num(video.get("like_count"))
    comments = _num(video.get("comment_count"))
    shares = _num(video.get("share_count"))
    return {
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "views": views,
        "impressions": views,
        "reach": views,
        "engagement_rate": round(((likes + comments + shares) / views) * 100, 2) if views else None,
        "post_url": video.get("share_url"),
        "cover_url": video.get("cover_image_url"),
        "duration": video.get("duration"),
    }
