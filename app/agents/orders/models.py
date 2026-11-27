"""Orders: the commercial agreement. Order, payment and fulfilment are three separate status
dimensions, and all money is stored as integer minor units (kobo, cents)."""
from __future__ import annotations

from typing import Any, Optional

ORDERS = "orders"
PAYMENTS = "order_payments"
REFUNDS = "order_refunds"
AUDIT = "order_audit"
COUNTERS = "order_counters"

DRAFT, AWAITING_CUSTOMER, CONFIRMED, IN_PROGRESS, COMPLETED, CANCELLED, EXPIRED = (
    "draft", "awaiting_customer", "confirmed", "in_progress", "completed", "cancelled", "expired")
ORDER_STATUSES = (DRAFT, AWAITING_CUSTOMER, CONFIRMED, IN_PROGRESS, COMPLETED, CANCELLED, EXPIRED)
EDITABLE = (DRAFT, AWAITING_CUSTOMER)
OPEN = (CONFIRMED, IN_PROGRESS)

UNPAID, PENDING, PARTIALLY_PAID, PAID, FAILED = "unpaid", "pending", "partially_paid", "paid", "failed"
REFUNDED, PARTIALLY_REFUNDED = "refunded", "partially_refunded"

NOT_STARTED, QUEUED, PROCESSING, PARTIALLY_FULFILLED, FULFILLED = (
    "not_started", "queued", "processing", "partially_fulfilled", "fulfilled")
DELIVERY_FAILED, RETURNED = "delivery_failed", "returned"
LINE_FULFILMENT = (QUEUED, PROCESSING, FULFILLED, DELIVERY_FAILED)

SOURCES = ("onebox", "dashboard", "api")
PAYMENT_SOURCES = ("provider", "merchant_recorded")
DEFAULT_PAYMENT_WINDOW_HOURS = 48
MAX_LINES = 20


class OrderError(Exception):
    def __init__(self, detail: str, status: int = 400, reason: str = "", **extra: Any):
        super().__init__(detail)
        self.detail, self.status, self.reason, self.extra = detail, status, reason, extra


def to_minor(amount: Optional[float]) -> Optional[int]:
    return None if amount is None else int(round(float(amount) * 100))


def positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise OrderError(f"{label} must be a whole number above zero.")
    return value
