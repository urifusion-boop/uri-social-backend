"""Uniqueness the order guarantees rest on: one order per idempotency key, one number per business,
one payment per provider transaction, one unguessable public token."""
from __future__ import annotations

from .models import ORDERS, PAYMENTS

_ready = False


async def ensure_indexes(db) -> None:
    global _ready
    if _ready:
        return
    spec = [
        (ORDERS, [("idem_scope", 1)], {"unique": True, "sparse": True, "name": "order_idem"}),
        (ORDERS, [("number_scope", 1)], {"unique": True, "sparse": True, "name": "order_number"}),
        (ORDERS, [("public_token", 1)], {"unique": True, "sparse": True, "name": "order_token"}),
        (ORDERS, [("business_id", 1), ("created_at", -1)], {"name": "order_recent"}),
        (PAYMENTS, [("txn_scope", 1)], {"unique": True, "sparse": True, "name": "payment_txn"}),
        (PAYMENTS, [("reference", 1)], {"unique": True, "sparse": True, "name": "payment_ref"}),
    ]
    for collection, keys, options in spec:
        try:
            await db[collection].create_index(keys, **options)
        except Exception as e:
            print(f"[Orders] index {options['name']} not created: {e}", flush=True)
    _ready = True


def reset() -> None:
    global _ready
    _ready = False
