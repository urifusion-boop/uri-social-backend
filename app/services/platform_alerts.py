"""
Platform-wide operational alerts — for failures that aren't any one user's
data problem but an infrastructure/account issue affecting everyone (e.g.
the OpenAI account running out of credit, which breaks every AI feature on
the platform at once). Deliberately not Sentry: nobody is actively watching
Sentry today, so an alert that only lives there is effectively invisible.
This is meant to actually be seen — surfaced on the Admin dashboard page an
admin already opens, plus an email (settings.PLATFORM_ALERT_EMAILS) so they
don't have to remember to go look.

One document per `kind` in `platform_alerts`, upserted on every occurrence
rather than one row per event — an outage that keeps failing for hours
should read as "ongoing since 9:04am, seen 340 times", not 340 separate
rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

RE_EMAIL_COOLDOWN_MINUTES = 60  # don't re-email on every failed call during a sustained outage


async def raise_alert(db, kind: str, title: str, detail: str, now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    existing = await db["platform_alerts"].find_one({"kind": kind})
    await db["platform_alerts"].update_one(
        {"kind": kind},
        {
            "$set": {"title": title, "detail": detail, "last_seen": now, "dismissed_at": None},
            "$setOnInsert": {"kind": kind, "first_seen": now},
            "$inc": {"occurrences": 1},
        },
        upsert=True,
    )
    last_emailed = (existing or {}).get("last_emailed_at")
    was_dismissed = bool((existing or {}).get("dismissed_at"))
    should_email = (
        existing is None
        or was_dismissed  # it came back after an admin marked it fixed — tell them again
        or not last_emailed
        or (now - last_emailed) >= timedelta(minutes=RE_EMAIL_COOLDOWN_MINUTES)
    )
    if should_email:
        await _email_admins(title, detail)
        await db["platform_alerts"].update_one({"kind": kind}, {"$set": {"last_emailed_at": now}})


async def dismiss_alert(db, kind: str) -> Optional[dict]:
    result = await db["platform_alerts"].update_one({"kind": kind}, {"$set": {"dismissed_at": datetime.utcnow()}})
    if result.matched_count == 0:
        return None
    doc = await db["platform_alerts"].find_one({"kind": kind})
    doc.pop("_id", None)
    return doc


async def list_alerts(db, include_dismissed: bool = False) -> list[dict]:
    q = {} if include_dismissed else {"dismissed_at": None}
    out = [a async for a in db["platform_alerts"].find(q).sort("last_seen", -1)]
    for a in out:
        a.pop("_id", None)
    return out


async def _email_admins(title: str, detail: str) -> None:
    from app.core.config import settings
    from app.services.EmailService import email_service

    admins = [e.strip() for e in (settings.PLATFORM_ALERT_EMAILS or "").split(",") if e.strip()]
    body = (
        f"<p><strong>{title}</strong></p>"
        f"<p>{detail}</p>"
        f"<p style=\"color:#888;font-size:12px\">This is an automated platform alert from URI Social. "
        f"It also appears on the Admin dashboard.</p>"
    )
    for addr in admins:
        try:
            await email_service.send_raw_email(addr, f"[URI Social] {title}", body)
        except Exception as e:
            print(f"[platform_alerts] failed to email {addr}: {e}")
