"""Catalog & Availability: item types, availability models and input validation.

One shared catalog; the availability model decides how "can I sell this?" is answered.
A pair of shoes, a SaaS plan, a tour seat and an appointment are never forced into one
quantity field — each availability type has its own handler in availability.py.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

ITEMS = "catalog_items"
VARIANTS = "catalog_variants"
STOCK = "catalog_stock"
SLOTS = "catalog_slots"
RESERVATIONS = "catalog_reservations"
MOVEMENTS = "catalog_movements"
EVENTS = "catalog_events"
DEMAND = "catalog_demand"
DRAFT_ORDERS = "catalog_draft_orders"
ALERT_LOG = "catalog_alert_log"

ITEM_TYPES = ("physical", "digital", "capacity", "service", "made_to_order")
AVAILABILITY_TYPES = ("quantity", "unlimited", "capped", "capacity", "schedule", "manual", "made_to_order")
ALLOWED_AVAILABILITY = {
    "physical": ("quantity", "unlimited", "manual"),
    "digital": ("unlimited", "capped", "manual"),
    "capacity": ("capacity", "manual"),
    "service": ("schedule", "capacity", "manual"),
    "made_to_order": ("made_to_order", "manual"),
}
DEFAULT_AVAILABILITY = {
    "physical": "quantity", "digital": "unlimited", "capacity": "capacity",
    "service": "schedule", "made_to_order": "made_to_order",
}
STATUSES = ("active", "inactive", "archived")
PRICING_MODELS = ("fixed", "quote")
BILLING_INTERVALS = ("one_time", "weekly", "monthly", "quarterly", "yearly")
PERIODS = ("total", "day", "week", "month")
STALE_FALLBACKS = ("require_confirmation", "assume_unavailable", "allow")
AVAILABILITY_SOURCES = ("uri", "manual", "external")

# Reservation states.
PENDING, AWAITING, CONFIRMED, FULFILLED = "pending", "awaiting_confirmation", "confirmed", "fulfilled"
RELEASED, EXPIRED, CANCELLED, DECLINED = "released", "expired", "cancelled", "declined"
ACTIVE_RESERVATION = (PENDING, AWAITING)
COMMITMENTS = (PENDING, AWAITING, CONFIRMED)         # promises the business must still keep

# Stock movements. Each changes on_hand, reserved, or both; the ledger reconciles to the counters.
MOVEMENT_TYPES = ("received", "sold", "reserved", "released", "damaged", "lost", "returned", "adjusted")
ADJUSTMENT_TYPES = ("received", "damaged", "lost", "returned", "adjusted")

DEFAULT_RESERVATION_MINUTES = 30
MAX_RESERVATION_MINUTES = 7 * 24 * 60
DEFAULT_TIMEZONE = "Africa/Lagos"
SKU_RE = re.compile(r"^[A-Za-z0-9._\-/ ]{1,64}$")


STOCK_VISIBILITY = ("exact", "low_only", "never")


class CatalogError(Exception):
    def __init__(self, detail: str, status: int = 400, reason: str = "", **extra: Any):
        super().__init__(detail)
        self.detail, self.status, self.reason, self.extra = detail, status, reason, extra


# ── time ─────────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    """Naive UTC, which is what Mongo hands back; API output adds the Z."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_db(value: Any) -> Optional[datetime]:
    """Naive UTC from a datetime or an ISO string (a trailing Z or offset is honoured)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise CatalogError(f"'{value}' is not a valid date and time.")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(microsecond=0).isoformat() + "Z" if isinstance(value, datetime) else None


# ── field helpers ────────────────────────────────────────────────────────────

def _text(raw: dict, key: str, limit: int, required: bool = False, label: str = "") -> str:
    value = str(raw.get(key) or "").strip()
    if required and not value:
        raise CatalogError(f"{label or key.replace('_', ' ').capitalize()} is required.")
    return value[:limit]


def _number(value: Any, label: str, minimum: float = 0, maximum: float = 1e12) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CatalogError(f"{label} must be a number.")
    if number != number or number < minimum or number > maximum:
        raise CatalogError(f"{label} must be between {minimum:g} and {maximum:g}.")
    return number


def _int(value: Any, label: str, minimum: int = 0, maximum: int = 10**9) -> int:
    number = _number(value, label, minimum, maximum)
    if int(number) != number:
        raise CatalogError(f"{label} must be a whole number.")
    return int(number)


def _opt_int(raw: dict, key: str, label: str, minimum: int = 0, maximum: int = 10**9) -> Optional[int]:
    value = raw.get(key)
    return None if value in (None, "") else _int(value, label, minimum, maximum)


def _choice(value: Any, options: tuple, label: str, default: Optional[str] = None) -> str:
    value = str(value or default or "").strip().lower()
    if value not in options:
        raise CatalogError(f"{label} must be one of: {', '.join(options)}.")
    return value


def _clock(value: Any, label: str) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if not match:
        raise CatalogError(f"{label} must look like 09:00.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _zone(value: Any) -> str:
    name = str(value or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(name)
    except Exception:
        raise CatalogError(f"'{name}' is not a known timezone.")
    return name


def _strings(value: Any, limit: int, each: int = 200) -> list[str]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[\n;]", value)]
    return [str(v).strip()[:each] for v in (value or []) if str(v).strip()][:limit]


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes", "y")


# ── availability configuration, per model ────────────────────────────────────

def normalize_config(kind: str, raw: Optional[dict]) -> dict:
    raw = dict(raw or {})
    if kind == "capped":
        return {"cap": _int(raw.get("cap"), "Sales cap", 1),
                "period": _choice(raw.get("period"), PERIODS, "Cap period", "month")}
    if kind == "made_to_order":
        return {"max_orders": _opt_int(raw, "max_orders", "Maximum orders", 0),
                "period": _choice(raw.get("period"), PERIODS, "Period", "month"),
                "lead_time_days": _opt_int(raw, "lead_time_days", "Lead time", 0, 3650),
                "deposit_percent": None if raw.get("deposit_percent") in (None, "")
                else _number(raw["deposit_percent"], "Deposit", 0, 100)}
    if kind == "schedule":
        days = sorted({_int(d, "Day", 0, 6) for d in (raw.get("days") if raw.get("days") is not None else range(5))})
        start, end = _clock(raw.get("start") or "09:00", "Opening time"), _clock(raw.get("end") or "17:00", "Closing time")
        if start >= end:
            raise CatalogError("Closing time must be after opening time.")
        return {
            "days": days or list(range(5)), "start": start, "end": end,
            "slot_minutes": _int(raw.get("slot_minutes") or 60, "Slot length", 5, 1440),
            "capacity_per_slot": _int(raw.get("capacity_per_slot") or 1, "Capacity per slot", 1, 100000),
            "buffer_minutes": _int(raw.get("buffer_minutes") or 0, "Buffer", 0, 1440),
            "advance_booking_days": _int(60 if raw.get("advance_booking_days") in (None, "") else raw["advance_booking_days"],
                                         "Advance booking limit", 0, 730),
            "min_notice_hours": _int(raw.get("min_notice_hours") or 0, "Minimum notice", 0, 24 * 60),
            "blackout_dates": sorted({d for d in _strings(raw.get("blackout_dates"), 400, 10)
                                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)}),
            "timezone": _zone(raw.get("timezone")),
            "cancellation_hours": _opt_int(raw, "cancellation_hours", "Cancellation window", 0, 24 * 365),
        }
    if kind == "capacity":
        return {"low_capacity_threshold": _opt_int(raw, "low_capacity_threshold", "Low capacity threshold", 0)}
    if kind == "manual":
        return {"available": raw.get("available") is None or _flag(raw.get("available")),
                "stale_after_hours": _int(24 if raw.get("stale_after_hours") in (None, "") else raw["stale_after_hours"],
                                          "Stale after", 1, 24 * 365),
                "stale_fallback": _choice(raw.get("stale_fallback"), STALE_FALLBACKS, "Stale fallback", "require_confirmation"),
                "note": str(raw.get("note") or "").strip()[:300]}
    return {}


def normalize_item(raw: dict, existing: Optional[dict] = None) -> dict:
    """The validated item fields, or CatalogError. `existing` makes it a patch: omitted
    fields keep their value, so a price edit cannot wipe the availability setup."""
    merged = {**(existing or {}), **{k: v for k, v in raw.items() if k in _ITEM_FIELDS}}
    kind = _choice(merged.get("item_type"), ITEM_TYPES, "Item type")
    availability = _choice(merged.get("availability_type"), ALLOWED_AVAILABILITY[kind], "Availability model",
                           DEFAULT_AVAILABILITY[kind])
    pricing = _choice(merged.get("pricing_model"), PRICING_MODELS, "Pricing model", "fixed")
    price = None
    if pricing == "fixed":
        if merged.get("price") in (None, ""):
            raise CatalogError("Selling price is required (or set pricing to quote-based).")
        price = _number(merged["price"], "Selling price")
    currency = str(merged.get("currency") or "NGN").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise CatalogError("Currency must be a 3-letter code such as NGN.")
    sku = _text(merged, "sku", 64)
    if sku and not SKU_RE.match(sku):
        raise CatalogError("SKU can use letters, numbers and . _ - / only.")
    previous = existing or {}
    if "availability_config" in raw:
        config_raw = raw["availability_config"]
    else:
        config_raw = previous.get("availability_config") if previous.get("availability_type") == availability else None
    config = normalize_config(availability, config_raw)
    cost = None if merged.get("cost_price") in (None, "") else _number(merged["cost_price"], "Cost price")
    # 0 means "hold until someone releases it": no automatic expiry.
    reservation = _int(DEFAULT_RESERVATION_MINUTES if merged.get("reservation_minutes") in (None, "")
                       else merged["reservation_minutes"], "Reservation time", 0, MAX_RESERVATION_MINUTES)
    source = _choice(merged.get("availability_source"), AVAILABILITY_SOURCES, "Availability source",
                     "manual" if availability == "manual" else "uri")
    return {
        "name": _text(merged, "name", 160, True, "Item name"),
        "description": _text(merged, "description", 4000, True, "Description"),
        "item_type": kind,
        "category": _text(merged, "category", 80, True, "Category"),
        "images": [u for u in _strings(merged.get("images"), 10, 600) if u.startswith(("http://", "https://", "/"))],
        "sku": sku,
        "pricing_model": pricing, "price": price, "currency": currency,
        "availability_type": availability, "availability_config": config,
        "availability_source": source,
        "external_provider": _text(merged, "external_provider", 80),
        "status": _choice(merged.get("status"), STATUSES, "Status", "active"),
        "sales_notes": _text(merged, "sales_notes", 2000),
        "cost_price": cost,
        "requires_confirmation": _flag(merged.get("requires_confirmation")) or availability == "manual",
        "reservation_minutes": reservation,
        "confirmation_hours": _int(24 if merged.get("confirmation_hours") in (None, "") else merged["confirmation_hours"],
                                   "Confirmation window", 1, 24 * 90),
        "billing_interval": _choice(merged.get("billing_interval"), BILLING_INTERVALS, "Billing interval", "one_time")
        if kind == "digital" else "",
        "plan_features": _strings(merged.get("plan_features"), 40) if kind == "digital" else [],
        "usage_limits": {str(k)[:60]: str(v)[:60] for k, v in list((merged.get("usage_limits") or {}).items())[:20]}
        if kind == "digital" else {},
        "trial_eligible": _flag(merged.get("trial_eligible")) if kind == "digital" else False,
        "cancellation_policy": _text(merged, "cancellation_policy", 600),
        "low_stock_threshold": _opt_int(merged, "low_stock_threshold", "Low stock threshold", 0),
        "stock_visibility": _choice(merged.get("stock_visibility"), STOCK_VISIBILITY, "Stock visibility", "low_only"),
    }


_ITEM_FIELDS = {
    "name", "description", "item_type", "category", "images", "sku", "pricing_model", "price", "currency",
    "availability_type", "availability_source", "external_provider", "status", "sales_notes", "cost_price",
    "requires_confirmation", "reservation_minutes", "confirmation_hours", "billing_interval", "plan_features",
    "stock_visibility",
    "usage_limits", "trial_eligible", "cancellation_policy", "low_stock_threshold",
}


def normalize_variant(raw: dict, existing: Optional[dict] = None) -> dict:
    merged = {**(existing or {}), **raw}
    sku = _text(merged, "sku", 64)
    if sku and not SKU_RE.match(sku):
        raise CatalogError("SKU can use letters, numbers and . _ - / only.")
    attrs = merged.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise CatalogError("Attributes must be name and value pairs, such as size: 42.")
    price = None if merged.get("price") in (None, "") else _number(merged["price"], "Variant price")
    currency = str(merged.get("currency") or "").strip().upper()
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise CatalogError("Currency must be a 3-letter code such as NGN.")
    return {
        "name": _text(merged, "name", 120, True, "Variant name"),
        "sku": sku,
        "attributes": {str(k).strip()[:40]: str(v).strip()[:80] for k, v in list(attrs.items())[:20] if str(k).strip()},
        "price": price, "currency": currency,
        "status": _choice(merged.get("status"), ("active", "inactive", "archived"), "Status", "active"),
    }


# ── periods for capped sales and production capacity ─────────────────────────

def period_key(period: str, at: datetime) -> str:
    if period == "total":
        return "total"
    if period == "day":
        return at.strftime("%Y-%m-%d")
    if period == "week":
        year, week, _ = at.isocalendar()
        return f"{year}-W{week:02d}"
    return at.strftime("%Y-%m")


def day_bounds(local_day: date, zone: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(zone)
    start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            (start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None))
