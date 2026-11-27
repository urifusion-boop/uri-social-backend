"""CSV in and out: bring a catalog over from a spreadsheet, take it back out for reports."""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional

from . import items as items_svc
from . import stock
from .models import ITEMS, STOCK, VARIANTS, CatalogError, normalize_item, normalize_variant

MAX_ROWS = 2000
ALIASES = {
    "title": "name", "product": "name", "product_name": "name", "item": "name", "item_name": "name",
    "type": "item_type", "stock": "quantity", "qty": "quantity", "inventory": "quantity", "on_hand": "quantity",
    "image": "image_url", "images": "image_url", "photo": "image_url", "selling_price": "price",
    "variant": "variant_name", "option": "variant_name", "variant_stock": "variant_quantity",
    "variant_qty": "variant_quantity", "options": "variant_attributes", "attributes": "variant_attributes",
    "availability": "availability_type", "low_stock": "low_stock_threshold",
}
TYPE_WORDS = {"product": "physical", "goods": "physical", "physical": "physical", "service": "service",
              "digital": "digital", "software": "digital", "course": "digital", "tour": "capacity",
              "capacity": "capacity", "event": "capacity", "custom": "made_to_order", "made_to_order": "made_to_order",
              "made to order": "made_to_order"}
EXPORT_COLUMNS = ["name", "description", "item_type", "category", "sku", "price", "currency", "pricing_model",
                  "availability_type", "status", "quantity", "low_stock_threshold", "variant_name", "variant_sku",
                  "variant_price", "variant_attributes", "variant_quantity"]


def _header(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return ALIASES.get(key, key)


def _money(value: Any) -> Any:
    text = re.sub(r"[^\d.\-]", "", str(value or ""))
    return text if text not in ("", "-", ".") else None


def _count(value: Any, label: str) -> Optional[int]:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        raise CatalogError(f"{label} '{value}' isn't a number.")
    if number < 0 or int(number) != number:
        raise CatalogError(f"{label} must be a whole number of 0 or more.")
    return int(number)


def _attributes(value: str) -> dict:
    pairs = [p.strip() for p in re.split(r"[;|]", value or "") if p.strip()]
    return {k.strip(): v.strip() for k, _, v in (p.partition("=") if "=" in p else p.partition(":") for p in pairs) if k.strip()}


def parse(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CatalogError("The file is empty.")
    rows = []
    for number, raw in enumerate(reader, start=2):
        row = {_header(k): (v or "").strip() for k, v in raw.items() if k}
        if any(row.values()):
            rows.append({"_row": number, **row})
    if len(rows) > MAX_ROWS:
        raise CatalogError(f"Import up to {MAX_ROWS} rows at a time.")
    if rows and "name" not in rows[0]:
        raise CatalogError("The file needs a 'name' column.")
    return rows


def _item_fields(row: dict) -> dict:
    kind = TYPE_WORDS.get(row.get("item_type", "").lower(), row.get("item_type", "").lower() or "physical")
    fields: dict[str, Any] = {"name": row.get("name"), "description": row.get("description") or row.get("name"),
                              "item_type": kind, "category": row.get("category") or "General"}
    for key in ("sku", "currency", "pricing_model", "status", "billing_interval", "availability_type", "sales_notes"):
        if row.get(key):
            fields[key] = row[key].lower() if key in ("pricing_model", "status", "availability_type") else row[key]
    price = _money(row.get("price"))
    if row.get("price") and price is None:
        raise CatalogError(f"Price '{row['price']}' isn't a number.")
    if price is not None:
        fields["price"] = price
    elif not row.get("pricing_model"):
        fields["pricing_model"] = "quote"
    if row.get("image_url"):
        fields["images"] = [u.strip() for u in re.split(r"[;|\s]+", row["image_url"]) if u.strip()]
    if row.get("low_stock_threshold"):
        fields["low_stock_threshold"] = _count(row["low_stock_threshold"], "Low stock threshold")
    return fields


async def _find_item(db, business_id: str, fields: dict) -> Optional[dict]:
    if fields.get("sku"):
        found = await db[ITEMS].find_one({"business_id": business_id, "sku": fields["sku"]})
        if found:
            return found
    return await db[ITEMS].find_one({"business_id": business_id, "name": {"$regex": f"^{re.escape(fields['name'])}$", "$options": "i"},
                                     "status": {"$ne": "archived"}})


async def import_csv(db, business_id: str, text: str, actor: str, dry_run: bool = False) -> dict:
    rows = parse(text)
    created = updated = 0
    errors: list[dict] = []
    groups: list[list[dict]] = []
    for row in rows:                      # a row with no name continues the item above it (more variants)
        if row.get("name") or not groups:
            groups.append([row])
        else:
            groups[-1].append(row)
    for group in groups:
        first = group[0]
        try:
            fields = _item_fields(first)
            existing = await _find_item(db, business_id, fields) if not dry_run or fields.get("name") else None
            normalize_item(fields, existing)
            wants_variants = any(r.get("variant_name") for r in group)
            if dry_run:
                for r in group:
                    if r.get("variant_name"):
                        normalize_variant({"name": r["variant_name"], "sku": r.get("variant_sku", ""),
                                           "price": _money(r.get("variant_price")),
                                           "attributes": _attributes(r.get("variant_attributes", ""))})
                    _count(r.get("variant_quantity") or r.get("quantity"), "Quantity")
                created += 0 if existing else 1
                updated += 1 if existing else 0
                continue
            quantity = _count(first.get("quantity"), "Quantity")
            if existing:
                item = await items_svc.update_item(db, business_id, existing["_id"], fields, actor)
                updated += 1
            else:
                item = await items_svc.create_item(db, business_id, {**fields, "initial_quantity": 0 if wants_variants else quantity}, actor)
                created += 1
            if item["availability_type"] == "quantity" and not wants_variants and quantity is not None and existing:
                await stock.set_on_hand(db, business_id, item["_id"], None, quantity, "CSV import", actor)
            for r in group:
                if r.get("variant_name"):
                    await _variant(db, business_id, item, r, actor)
        except CatalogError as e:
            errors.append({"row": first["_row"], "name": first.get("name", ""), "error": e.detail})
        except Exception as e:                       # one bad row must not stop the rest
            errors.append({"row": first["_row"], "name": first.get("name", ""), "error": str(e)[:200]})
    return {"dry_run": dry_run, "created": created, "updated": updated, "errors": errors, "rows": len(rows)}


async def _variant(db, business_id: str, item: dict, row: dict, actor: str) -> None:
    raw = {"name": row["variant_name"], "sku": row.get("variant_sku", ""), "price": _money(row.get("variant_price")),
           "attributes": _attributes(row.get("variant_attributes", ""))}
    qty = _count(row.get("variant_quantity") or row.get("quantity"), "Variant quantity")
    existing = None
    if raw["sku"]:
        existing = await db[VARIANTS].find_one({"business_id": business_id, "sku": raw["sku"]})
    existing = existing or await db[VARIANTS].find_one({"business_id": business_id, "item_id": item["_id"],
                                                        "name": raw["name"], "status": {"$ne": "archived"}})
    if existing:
        variant = await items_svc.update_variant(db, business_id, item["_id"], existing["_id"], raw, actor)
        if item["availability_type"] == "quantity" and qty is not None:
            await stock.set_on_hand(db, business_id, item["_id"], variant["_id"], qty, "CSV import", actor)
    else:
        await items_svc.create_variant(db, business_id, item["_id"], {**raw, "initial_quantity": qty or 0}, actor)


async def export_csv(db, business_id: str) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for item in await db[ITEMS].find({"business_id": business_id}).sort("name", 1).to_list(20000):
        base = {"name": item["name"], "description": item["description"], "item_type": item["item_type"],
                "category": item["category"], "sku": item.get("sku", ""), "price": item.get("price") if item.get("price") is not None else "",
                "currency": item["currency"], "pricing_model": item["pricing_model"],
                "availability_type": item["availability_type"], "status": item["status"],
                "low_stock_threshold": item.get("low_stock_threshold") if item.get("low_stock_threshold") is not None else ""}
        variants = await db[VARIANTS].find({"business_id": business_id, "item_id": item["_id"],
                                            "status": {"$ne": "archived"}}).to_list(2000)
        stocks = {s.get("variant_id"): s for s in await db[STOCK].find(
            {"business_id": business_id, "item_id": item["_id"], "period_key": ""}).to_list(2000)}
        if not variants:
            writer.writerow({**base, "quantity": stocks.get(None, {}).get("on_hand", "")})
            continue
        for i, v in enumerate(variants):
            writer.writerow({**(base if i == 0 else {"name": ""}), "variant_name": v["name"], "variant_sku": v.get("sku", ""),
                             "variant_price": v["price"] if v.get("price") is not None else "",
                             "variant_attributes": ";".join(f"{k}={val}" for k, val in v["attributes"].items()),
                             "variant_quantity": stocks.get(v["_id"], {}).get("on_hand", "")})
    return out.getvalue()
