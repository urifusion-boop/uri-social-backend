"""Appointment times: weekly opening hours turned into bookable slots on demand."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import stock
from .models import CatalogError, utcnow

MAX_WINDOW_DAYS = 62


def _utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def local_day(item: dict, at: datetime) -> date:
    zone = ZoneInfo((item.get("availability_config") or {}).get("timezone", "Africa/Lagos"))
    return at.replace(tzinfo=timezone.utc).astimezone(zone).date()


def slot_times(item: dict, day: date) -> list[tuple[datetime, datetime]]:
    """Every (start, end) in UTC the opening hours give this local day."""
    cfg = item["availability_config"]
    if day.weekday() not in cfg["days"] or day.isoformat() in cfg["blackout_dates"]:
        return []
    tz = ZoneInfo(cfg["timezone"])
    sh, sm = map(int, cfg["start"].split(":"))
    eh, em = map(int, cfg["end"].split(":"))
    cursor = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
    closing = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
    length, gap = timedelta(minutes=cfg["slot_minutes"]), timedelta(minutes=cfg["buffer_minutes"])
    out = []
    while cursor + length <= closing:
        out.append((_utc(cursor), _utc(cursor + length)))
        cursor += length + gap
    return out


def bookable_window(item: dict, now: datetime) -> tuple[datetime, datetime]:
    cfg = item["availability_config"]
    return (now + timedelta(hours=cfg["min_notice_hours"]),
            now + timedelta(days=cfg["advance_booking_days"] + 1))


async def ensure_slots(db, item: dict, first: date, last: date, now: Optional[datetime] = None) -> None:
    """Create the slots for this range that don't exist yet (idempotent, bounded)."""
    now = now or utcnow()
    if (last - first).days > MAX_WINDOW_DAYS:
        raise CatalogError(f"Ask for at most {MAX_WINDOW_DAYS} days at a time.")
    cfg = item["availability_config"]
    earliest, latest = bookable_window(item, now)
    day = first
    while day <= last:
        for start, end in slot_times(item, day):
            if earliest <= start <= latest:
                await stock.upsert_slot(db, item["business_id"], item["_id"], start, end, cfg["capacity_per_slot"])
        day += timedelta(days=1)


def bookable(item: dict, slot: dict, quantity: int, now: datetime) -> bool:
    if slot["status"] != "open" or slot["remaining"] < quantity:
        return False
    if item["availability_type"] != "schedule":
        return slot["start_at"] > now
    earliest, latest = bookable_window(item, now)
    return earliest <= slot["start_at"] <= latest
