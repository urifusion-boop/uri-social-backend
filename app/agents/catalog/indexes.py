"""Indexes the catalog's guarantees rely on (unique ones sit on one scoped field that only exists when
there is a SKU or key: a sparse index that also holds business_id would still index every row): unique SKUs, one slot per time, one stock row per
variant and period, one reservation per idempotency key, one event per dedupe key."""
from __future__ import annotations

from .models import DEMAND, EVENTS, ITEMS, MOVEMENTS, RESERVATIONS, SLOTS, STOCK, VARIANTS

_ready = False


async def ensure_indexes(db) -> None:
    global _ready
    if _ready:
        return
    spec = [
        (ITEMS, [("sku_scope", 1)], {"unique": True, "sparse": True, "name": "item_sku"}),
        (ITEMS, [("business_id", 1), ("status", 1), ("updated_at", -1)], {"name": "biz_status"}),
        (VARIANTS, [("sku_scope", 1)], {"unique": True, "sparse": True, "name": "variant_sku"}),
        (VARIANTS, [("item_id", 1)], {"name": "by_item"}),
        (STOCK, [("business_id", 1), ("item_id", 1), ("variant_id", 1), ("location_id", 1), ("period_key", 1)],
         {"unique": True, "name": "stock_key"}),
        (SLOTS, [("item_id", 1), ("variant_id", 1), ("resource_id", 1), ("start_at", 1)],
         {"unique": True, "name": "slot_key"}),
        (SLOTS, [("business_id", 1), ("item_id", 1), ("start_at", 1)], {"name": "slot_lookup"}),
        (RESERVATIONS, [("idem_scope", 1)], {"unique": True, "sparse": True, "name": "res_idem"}),
        (RESERVATIONS, [("status", 1), ("expires_at", 1)], {"name": "res_expiry"}),
        (RESERVATIONS, [("business_id", 1), ("item_id", 1), ("created_at", -1)], {"name": "res_item"}),
        (MOVEMENTS, [("business_id", 1), ("item_id", 1), ("created_at", -1)], {"name": "mv_item"}),
        (EVENTS, [("dedupe_key", 1)], {"unique": True, "sparse": True, "name": "ev_dedupe"}),
        (EVENTS, [("delivered", 1), ("created_at", 1)], {"name": "ev_pending"}),
        (DEMAND, [("dedupe_key", 1)], {"unique": True, "sparse": True, "name": "demand_dedupe"}),
    ]
    for collection, keys, options in spec:
        try:
            await db[collection].create_index(keys, **options)
        except Exception as e:                  # an index that can't be built must not stop the service
            print(f"[Catalog] index {options['name']} not created: {e}", flush=True)
    _ready = True


def reset() -> None:
    global _ready
    _ready = False
