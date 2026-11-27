"""What the AI sales agent may do with the catalog: read it, check it, and — only after a customer
confirms — reserve. It can never create items, change prices, add stock or override availability;
those calls don't exist here, and every write still goes through the same validated service code.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Optional

from . import availability, demand, reservations
from .items import effective_price, get_item, list_variants, serialise
from .models import DRAFT_ORDERS, ITEMS, VARIANTS, CatalogError, utcnow

AI_ACTOR = "uri-agent"
STOP = set("""the a an and or of to for in on at is are was were be do does did you your yours we our it its this that
these those i me my can could would will shall have has had any some with from about there how what when where which who
whom why please pls hi hello hey thanks thank much many get got want need like just also not no yes if so but as by
available availability price cost tell more info information""".split())
CURRENCY = {"NGN": "₦", "USD": "$", "GBP": "£", "EUR": "€", "GHS": "GH₵", "KES": "KSh "}
KINDS = {"physical": "product", "digital": "digital product", "capacity": "booking with limited places",
         "service": "service", "made_to_order": "made to order"}


def _tokens(text: str, keep_short: bool = False) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in STOP and (len(w) >= 3 or (keep_short and len(w) >= 2))}


def money(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return "price on request (the team gives a quote)"
    symbol = CURRENCY.get(currency, f"{currency} ")
    return f"{symbol}{amount:,.0f}" if float(amount).is_integer() else f"{symbol}{amount:,.2f}"


async def search(db, business_id: str, query: str, limit: int = 3) -> list[dict]:
    """Active items whose name, SKU, options or category the words point at, best first."""
    words = _tokens(query)
    short = _tokens(query, keep_short=True)
    if not words and not short:
        return []
    items = await db[ITEMS].find({"business_id": business_id, "status": "active"}).to_list(800)
    if not items:
        return []
    variants: dict[str, list[dict]] = {}
    for v in await db[VARIANTS].find({"business_id": business_id, "status": "active"}).to_list(8000):
        variants.setdefault(v["item_id"], []).append(v)
    text = (query or "").lower()
    scored = []
    for item in items:
        name = _tokens(item["name"], True)
        score = 5 * len(words & name)
        if len(item["name"]) > 2 and item["name"].lower() in text:
            score += 10
        if item.get("sku") and item["sku"].lower() in text:
            score += 8
        option_words = set()
        for v in variants.get(item["_id"], []):
            option_words |= _tokens(v["name"], True) | {w for val in v["attributes"].values() for w in _tokens(val, True)}
        score_options = 3 * len(short & option_words) if score else 0       # an option alone never picks the item
        score += score_options + 2 * len(words & _tokens(item["category"], True))
        if score >= 2:
            scored.append((score + len(words & _tokens(item["description"])) * 0.1, item))
    scored.sort(key=lambda p: -p[0])
    return [i for _, i in scored[:limit]]


async def alternatives(db, business_id: str, item: dict, limit: int = 3) -> list[dict]:
    """Other items in the same category that can be sold right now."""
    out = []
    for other in await db[ITEMS].find({"business_id": business_id, "status": "active", "category": item["category"],
                                       "_id": {"$ne": item["_id"]}}).to_list(40):
        try:
            res = await availability.check(db, business_id, other["_id"])
        except CatalogError:
            continue
        if res["available"] and not res.get("stale"):
            price, currency = effective_price(other)
            out.append({"item_id": other["_id"], "name": other["name"], "price": price, "currency": currency})
        if len(out) >= limit:
            break
    return out


def _threshold(item: dict) -> Optional[int]:
    configured = item.get("low_stock_threshold")
    return configured if configured is not None else (item.get("availability_config") or {}).get("low_capacity_threshold")


def stock_phrase(item: dict, quantity: Optional[int]) -> str:
    """What a customer may be told about how much is left, per the owner's stock visibility choice."""
    if quantity is None:
        return ""
    if quantity <= 0:
        return "out of stock"
    mode = item.get("stock_visibility") or "low_only"
    if mode == "exact":
        return f"{quantity} available"
    threshold = _threshold(item)
    return "limited stock" if mode == "low_only" and threshold is not None and quantity <= threshold else "in stock"


def customer_message(item: dict, res: dict) -> str:
    """The availability sentence with the count removed unless the owner chose to show it."""
    if (item.get("stock_visibility") or "low_only") == "exact":
        return res["message"]
    if res.get("reason") == "not_enough_stock":
        return "There isn't enough available for that quantity."
    quantity = res.get("available_quantity")
    if res["available"] and quantity is not None and not res.get("requires_confirmation"):
        return "Available, but stock is limited." if stock_phrase(item, quantity) == "limited stock" else "Available."
    return res["message"]


def _slots_line(item: dict, res: dict) -> str:
    slots = res.get("slots") or []
    if not slots:
        return ""
    shown = "; ".join(
        f"{s['start_at'][:16].replace('T', ' ')} UTC ({stock_phrase(item, s['remaining']).replace('available', 'left')})"
        for s in slots[:5])
    return f" Next open times: {shown}."


def _facts(item: dict, res: dict, asked: Optional[dict], alts: list[dict]) -> str:
    price, currency = effective_price(item)
    parts = [f"CATALOG ITEM: {item['name']} ({KINDS.get(item['item_type'], item['item_type'])}, {item['category']}).",
             f"Price: {money(price, currency)}.", f"About: {item['description'][:300]}"]
    if item["item_type"] == "digital":
        if item.get("billing_interval") and item["billing_interval"] != "one_time":
            parts.append(f"Billed {item['billing_interval']}.")
        if item.get("plan_features"):
            parts.append("Includes: " + "; ".join(item["plan_features"][:10]) + ".")
        if item.get("trial_eligible"):
            parts.append("A trial is available.")
    status = f"Availability (checked live just now): {customer_message(item, res)}"
    if res.get("variant_required") and res.get("variants"):
        rows = []
        for row in res["variants"][:12]:
            vp = row.get("price")
            label = f"{row['name']}: " + (stock_phrase(item, row["available_quantity"]) if row["available"] else "out of stock")
            rows.append(label + (f" at {money(vp, currency)}" if vp is not None and vp != price else ""))
        status += " By option: " + "; ".join(rows) + "."
    parts.append(status + _slots_line(item, res))
    if asked:
        parts.append(f"The option the customer named, {asked['name']}: {customer_message(item, asked['res'])}")
    if res.get("source") == "manual" and res.get("last_updated"):
        parts.append(f"This availability is kept by hand and was last updated {res['last_updated'][:10]}; it is not a live provider feed.")
    if res.get("requires_confirmation"):
        parts.append("The team must confirm this before it is booked. Never tell the customer it is booked or reserved.")
    if res.get("lead_time_days") is not None:
        parts.append(f"Usual lead time: {res['lead_time_days']} days.")
    if res.get("deposit_percent") is not None:
        parts.append(f"Deposit: {res['deposit_percent']:g}%.")
    if item.get("cancellation_policy"):
        parts.append(f"Cancellation: {item['cancellation_policy']}")
    if not res["available"] and alts:
        parts.append("Available alternatives: " + "; ".join(
            f"{a['name']} ({money(a['price'], a['currency'])})" for a in alts) + ".")
    if item.get("sales_notes"):
        parts.append(f"Internal guidance for you, never quote it: {item['sales_notes'][:500]}")
    return " ".join(parts)


async def catalog_sources(db, business_id: str, text: str, earlier: Optional[list[dict]] = None,
                          message_id: str = "", now: Optional[datetime] = None) -> list[dict]:
    """Live catalog entries for the items a customer's message is about, as citable sources.

    These are structured facts the business entered, checked at this moment, so they are
    marked approved: a price or stock answer cited from one needs no further review. If an
    item was asked for and can't be sold, the enquiry is recorded as missed demand.
    """
    now = now or utcnow()
    matched = await search(db, business_id, text)
    if not matched and earlier:                 # "do you have it in 43?" — the item came up earlier
        recent = " ".join(m["text"] for m in earlier if m.get("direction") == "inbound")[-400:]
        matched = await search(db, business_id, f"{recent} {text}")
    if not matched:
        return []
    out = []
    for item in matched:
        variant = await _variant_named(db, business_id, item, text)
        try:
            res = await availability.check(db, business_id, item["_id"], now=now)
            asked = {"name": variant["name"], "res": await availability.check(db, business_id, item["_id"], variant["_id"], now=now)} \
                if variant else None
        except CatalogError:
            continue
        unavailable = not (asked["res"] if asked else res)["available"]
        alts = [] if not unavailable or asked else await alternatives(db, business_id, item)
        if unavailable and message_id:
            await demand.record(db, business_id, item["_id"], variant["_id"] if variant else None,
                                (asked["res"] if asked else res).get("reason") or "unavailable", text, "onebox",
                                f"{message_id}:{item['_id']}")
        out.append({"id": f"catalog:{item['_id']}", "label": f"Catalog · {item['name']}",
                    "text": _facts(item, res, asked, alts), "approved": True, "price_or_stock": False})
    return out


_SIZES = {"small": "s", "medium": "m", "large": "l"}


def _option_words(text: str) -> set[str]:
    """Words naming an option. Sizes match however written (large = L), and "I'm" is not size M."""
    t = re.sub(r"['’]\w*", "", (text or "").lower())
    t = re.sub(r"\b(?:extra|x)[\s-]+(small|large)\b", lambda m: "x" + m.group(1)[0], t)
    words = [_SIZES.get(w, w) for w in re.findall(r"[a-z0-9]+", t)]
    return {w for w in words if w not in STOP and (len(w) >= 2 or w in ("s", "m", "l"))}


async def _variant_named(db, business_id: str, item: dict, text: str) -> Optional[dict]:
    """The option the customer named, when exactly one fits (e.g. "black size 42")."""
    words = _option_words(text)
    best, best_score, tie = None, 0, False
    for v in await list_variants(db, business_id, item["_id"]):
        if v["status"] != "active":
            continue
        have = _option_words(v["name"]) | {w for val in v["attributes"].values() for w in _option_words(str(val))}
        score = len(words & have)
        if score > best_score:
            best, best_score, tie = v, score, False
        elif score == best_score and score:
            tie = True
    return best if best and best_score and not tie else None


# ── tools the agent calls through the API ────────────────────────────────────

async def ai_search(db, business_id: str, query: str, limit: int = 5) -> list[dict]:
    out = []
    for item in await search(db, business_id, query, max(1, min(limit, 10))):
        price, currency = effective_price(item)
        out.append({**serialise(item, "customer"), "display_price": money(price, currency)})
    return out


async def ai_details(db, business_id: str, item_id: str) -> dict:
    item = await get_item(db, business_id, item_id)
    if item["status"] != "active":
        raise CatalogError("Item not found.", 404)
    out = serialise(item, "customer")
    out["variants"] = [serialise(v, "customer") for v in await list_variants(db, business_id, item_id)]
    return out


async def ai_check(db, business_id: str, item_id: str, variant_id: Optional[str] = None, quantity: int = 1,
                   start_at: Any = None, end_at: Any = None) -> dict:
    res = await availability.check(db, business_id, item_id, variant_id, quantity, start_at, end_at)
    item = await get_item(db, business_id, item_id)
    return _hide_counts(item, res)


def _hide_counts(item: dict, res: dict) -> dict:
    if (item.get("stock_visibility") or "low_only") == "exact":
        return res
    shown = {**res, "message": customer_message(item, res), "available_quantity": None}
    shown["variants"] = [{**v, "available_quantity": stock_phrase(item, v["available_quantity"])}
                         for v in res.get("variants") or []]
    shown["slots"] = [{k: v for k, v in slot.items() if k not in ("remaining", "total")} for slot in res.get("slots") or []]
    return shown


async def ai_compare(db, business_id: str, item_ids: list[str]) -> list[dict]:
    out = []
    for item_id in item_ids[:5]:
        item = await get_item(db, business_id, item_id)
        res = await availability.check(db, business_id, item_id)
        price, currency = effective_price(item)
        out.append({"item_id": item_id, "name": item["name"], "price": price, "currency": currency,
                    "available": res["available"], "message": customer_message(item, res)})
    return out


async def ai_alternatives(db, business_id: str, item_id: str) -> list[dict]:
    return await alternatives(db, business_id, await get_item(db, business_id, item_id))


# ── draft orders: what the agent assembles before the customer confirms ──────

async def create_draft_order(db, business_id: str, lines: list[dict], customer: Optional[dict] = None,
                             actor: str = AI_ACTOR) -> dict:
    """Prices come from the catalog, never from the caller. Nothing is reserved yet."""
    if not lines or len(lines) > 20:
        raise CatalogError("Add between 1 and 20 items to the order.")
    built, total, quote = [], 0.0, False
    for line in lines:
        quantity = line.get("quantity", 1)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise CatalogError("Quantity must be at least 1.")
        item = await get_item(db, business_id, line.get("item_id", ""))
        if item["status"] != "active":
            raise CatalogError(f"{item['name']} isn't for sale right now.", 409, "not_for_sale")
        variant = None
        if line.get("variant_id"):
            variant = next((v for v in await list_variants(db, business_id, item["_id"]) if v["_id"] == line["variant_id"]), None)
            if not variant:
                raise CatalogError("That option doesn't exist.", 404)
        price, currency = effective_price(item, variant)
        if price is None:
            quote = True
        else:
            total += price * quantity
        built.append({"item_id": item["_id"], "variant_id": line.get("variant_id"), "name": item["name"] + (f" ({variant['name']})" if variant else ""),
                      "quantity": quantity, "unit_price": price, "currency": currency, "start_at": line.get("start_at"),
                      "end_at": line.get("end_at")})
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "lines": built, "customer": customer or {},
           "status": "draft", "total": None if quote else total, "needs_quote": quote,
           "currency": built[0]["currency"], "created_by": actor, "created_at": utcnow(), "reservation_ids": []}
    await db[DRAFT_ORDERS].insert_one(doc)
    return doc


async def get_draft_order(db, business_id: str, draft_id: str) -> dict:
    doc = await db[DRAFT_ORDERS].find_one({"_id": draft_id, "business_id": business_id})
    if not doc:
        raise CatalogError("Order not found.", 404)
    return doc


async def reserve_draft_order(db, business_id: str, draft_id: str, customer_confirmed: bool,
                              actor: str = AI_ACTOR) -> dict:
    """Reserve every line, or none. Only after the customer has said yes."""
    if customer_confirmed is not True:
        raise CatalogError("The customer must confirm the order before anything is reserved.", 400, "not_confirmed")
    draft = await get_draft_order(db, business_id, draft_id)
    if draft["status"] == "reserved":
        return draft
    if draft["status"] != "draft":
        raise CatalogError(f"This order is {draft['status']}.", 409)
    made: list[dict] = []
    try:
        for index, line in enumerate(draft["lines"]):
            made.append(await reservations.reserve(
                db, business_id, line["item_id"], line["quantity"], line.get("variant_id"), order_id=draft_id,
                start_at=line.get("start_at"), end_at=line.get("end_at"), actor=actor,
                idempotency_key=f"draft:{draft_id}:{index}", source="onebox", customer=draft.get("customer")))
    except CatalogError as e:
        for res in made:
            await reservations.release(db, business_id, res["_id"], "Order could not be fully reserved", actor)
        raise CatalogError(f"{draft['lines'][len(made)]['name']}: {e.detail}", e.status, e.reason, **e.extra)
    await db[DRAFT_ORDERS].update_one({"_id": draft_id}, {"$set": {"status": "reserved", "reservation_ids": [r["_id"] for r in made],
                                                                    "reserved_at": utcnow()}})
    return await get_draft_order(db, business_id, draft_id)


async def release_draft_order(db, business_id: str, draft_id: str, actor: str = AI_ACTOR) -> dict:
    draft = await get_draft_order(db, business_id, draft_id)
    for rid in draft.get("reservation_ids", []):
        await reservations.release(db, business_id, rid, "Order cancelled", actor)
    await db[DRAFT_ORDERS].update_one({"_id": draft_id}, {"$set": {"status": "cancelled"}})
    return await get_draft_order(db, business_id, draft_id)
