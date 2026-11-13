"""
Website → Brand Playbook.

read_site_job (a background job) reads the business's site and extracts Playbook fields.
compare() lines those findings up against what the Playbook already says, and apply() saves
only the fields the owner picked, recording that each came from the website.

Built to stay cheap at scale: a site is read once per day at most and the result is shared by
every brand that names the same site (sites_cache), each brand can start a limited number of
reads a day, and the whole read is capped in pages, bytes and time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from app.jobs import queue
from app.jobs.worker import JobError

from . import extractor
from .html_read import add_colors, pick_pages, read_page, top_colors
from .safe_fetch import TIMEOUT, USER_AGENT, FetchError, fetch, normalize_url, robots_permits, site_key

JOB_TYPE = "website_read"
SITES = "brand_intel_sites"
CACHE_FRESH = timedelta(hours=24)
CACHE_TTL_SECONDS = 7 * 24 * 3600
READS_PER_BRAND_PER_DAY = 10
MAX_EXTRA_PAGES = 9
PAGE_TEXT_CHARS = 5_000
READ_DEADLINE_SECONDS = 75

_KIND_LABELS = {"about": "About", "services": "Services", "products": "Products", "pricing": "Pricing",
                "contact": "Contact", "faq": "FAQ", "reviews": "Reviews"}

# Labels and sections the Playbook already uses, plus the fields only this feature fills.
_EXTRA_FIELDS = {
    "derived_voice": ("Brand voice", "Brand Voice"),
    "social_links": ("Social links", "Brand Identity"),
    "customer_location": ("Areas you serve", "Audience"),
}
_TEXT_FIELDS = {k for k, (kind, _) in extractor.FIELDS.items() if kind == "text"} | {"website"}
_LIST_FIELDS = {k for k, (kind, _) in extractor.FIELDS.items() if kind == "list"} | {"brand_colors"}


async def ensure_indexes(db) -> None:
    await db[SITES].create_index("site", unique=True, name="site_unique")
    await db[SITES].create_index("fetched_at", expireAfterSeconds=CACHE_TTL_SECONDS, name="fetched_ttl")


# ── starting a read ────────────────────────────────────────────────────────────

async def start_read(db, *, brand_id: str, user_id: str, url: str, force: bool = False) -> dict:
    """Queue a read of `url` for this brand. Asking again while one is running returns that one."""
    normalized = normalize_url(url)  # FetchError → the router turns it into a 400 with this message
    since = datetime.now(timezone.utc) - timedelta(days=1)
    recent = await db[queue.COLLECTION].count_documents(
        {"brand_id": brand_id, "type": JOB_TYPE, "created_at": {"$gte": since}})
    if recent >= READS_PER_BRAND_PER_DAY:
        raise FetchError("You've read your website a lot today. Try again tomorrow.", "rate_limited")
    return await queue.enqueue(
        db, JOB_TYPE, {"url": normalized, "force": force},
        brand_id=brand_id, user_id=user_id, key=f"{brand_id}:{site_key(normalized)}", max_attempts=3)


# ── the job ────────────────────────────────────────────────────────────────────

async def read_site_job(db, job: dict, ctx) -> dict:
    try:
        url = normalize_url(job["payload"]["url"])
    except FetchError as e:
        raise JobError(e.message, code=e.code)
    key = site_key(url)
    if not job["payload"].get("force"):
        cached = await db[SITES].find_one({"site": key}, {"_id": 0})
        if cached and _fresh(cached.get("fetched_at")):
            await ctx.step("cache", f"Read {key} earlier today", "done")
            await ctx.found(_found_preview(cached.get("fields") or {}))
            return _result(cached)

    await ctx.step("check", f"Checking {key}")
    try:
        site = await asyncio.wait_for(_crawl(url, ctx), timeout=READ_DEADLINE_SECONDS + 15)
    except FetchError as e:
        raise JobError(e.message, code=e.code, retry=e.code in ("timeout", "connect"))
    except asyncio.TimeoutError:
        raise JobError("Your website took too long to read. Try again in a few minutes.", code="timeout", retry=True)

    await ctx.step("understand", "Understanding your business")
    try:
        fields = await extractor.extract(site)
    except extractor.ExtractionUnavailable as e:
        print(f"[brand_intel] extraction failed for {key}: {e}", flush=True)
        raise JobError("We read your website but couldn't make sense of it just now. Try again shortly.",
                       code="ai_unavailable", retry=True)
    fields.update(_deterministic_fields(site, fields))
    await ctx.step("understand", "Understanding your business", "done", f"Found {len(fields)} things")
    await ctx.found(_found_preview(fields))

    doc = {"site": key, "url": site["url"], "fetched_at": datetime.now(timezone.utc),
           "fields": fields, "pages": site["page_list"], "logo_candidates": site.get("logo_candidates", [])[:5]}
    await db[SITES].update_one({"site": key}, {"$set": doc}, upsert=True)
    await _tell_owner(db, job, key, len(fields))
    return _result(doc)


async def _tell_owner(db, job: dict, site: str, count: int) -> None:
    """The bell: the owner may have left the page while the site was being read."""
    if not job.get("user_id"):
        return
    try:
        from app.services import brand_notifier
        await brand_notifier.notify_in_app(
            db, job["user_id"], job.get("brand_id"), "playbook_ready",
            title="Your Playbook is ready to check",
            message=f"Jane read {site} and found {count} things for your Brand Playbook. Check them before they're used.",
            link="/workspace/?tab=playbook&website_read=1",
            metadata={"read_id": job.get("job_id")})
    except Exception as e:  # a notification must never fail the read
        print(f"[brand_intel] notification failed: {e!r}", flush=True)


async def _crawl(url: str, ctx) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"}) as client:
        robots = ""
        parts = urlsplit(url)
        try:
            robots = (await fetch(f"{parts.scheme}://{parts.netloc}/robots.txt", accept=("text/plain", "text/html"),
                                  client=client, max_bytes=200_000)).text
        except FetchError as e:
            if e.code in ("dns", "private"):
                raise
        if robots and not robots_permits(robots, parts.path or "/"):
            raise FetchError("Your website asks tools not to read it, so we didn't.", "robots")
        await ctx.step("check", f"Checking {site_key(url)}", "done")

        await ctx.step("home", "Reading your home page")
        home_page = await fetch(url, client=client)
        home = read_page(home_page.text, home_page.url)
        await ctx.step("home", "Reading your home page", "done", home.title[:80])
        await ctx.found([h for h in [home.og.get("site_name") or home.title] if h])

        picks = [(k, u) for k, u in pick_pages(home, MAX_EXTRA_PAGES)
                 if not robots or robots_permits(robots, urlsplit(u).path or "/")]
        reads = [("home", home)]
        if picks:
            names = ", ".join(dict.fromkeys(_KIND_LABELS.get(k, k.title()) for k, _ in picks))
            await ctx.step("pages", f"Reading {names}")
            sem = asyncio.Semaphore(3)

            async def one(kind: str, u: str):
                async with sem:
                    try:
                        p = await fetch(u, client=client)
                        return kind, read_page(p.text, p.url)
                    except FetchError:
                        return None

            done = await asyncio.gather(*(one(k, u) for k, u in picks))
            reads += [d for d in done if d]
            await ctx.step("pages", f"Reading {names}", "done", f"{len(reads) - 1} of {len(picks)} pages read")

        colors_counter = home.colors.copy()
        for css in [s for s in home.stylesheets if site_key(s) == site_key(url)][:2]:
            try:
                sheet = await fetch(css, accept=("text/css",), client=client, max_bytes=300_000)
                add_colors(colors_counter, sheet.text)
            except FetchError:
                continue

    return _compose(url, home_page.url, reads, colors_counter, home)


def _compose(requested: str, final_url: str, reads: list, colors_counter, home) -> dict:
    blocks, socials, jsonld, pages = [], {}, [], []
    for kind, r in reads:
        pages.append(r.url)
        socials = {**r.socials, **socials}
        jsonld += r.jsonld
        head = [f"=== PAGE ({kind}): {r.url}", f"TITLE: {r.title}"]
        if r.description:
            head.append(f"DESCRIPTION: {r.description}")
        if r.headings:
            head.append("HEADINGS: " + " | ".join(r.headings[:15]))
        blocks.append("\n".join(head) + "\n" + r.text[:PAGE_TEXT_CHARS])
    if jsonld:
        import json as _json
        blocks.insert(0, "=== STRUCTURED BUSINESS DATA\n" + _json.dumps(jsonld[:3], ensure_ascii=False)[:3000])
    return {
        "url": final_url, "requested": requested, "text": "\n\n".join(blocks), "pages": pages,
        "page_list": [{"kind": k, "url": r.url, "title": r.title} for k, r in reads],
        "socials": socials, "colors": top_colors(colors_counter, home.theme_color),
        "logo_candidates": home.logo_candidates, "jsonld": jsonld[:3], "lang": home.lang,
    }


def _deterministic_fields(site: dict, fields: dict) -> dict:
    """Facts read straight off the page, not from the model."""
    out: dict[str, dict[str, Any]] = {"website": {"value": site["url"], "basis": "stated", "page": site["url"]}}
    if site.get("socials"):
        out["social_links"] = {"value": dict(site["socials"]), "basis": "stated", "page": site["url"]}
    if site.get("colors"):
        out["brand_colors"] = {"value": site["colors"], "basis": "inferred", "page": site["url"]}
    if "brand_name" not in fields:
        name = next((j.get("name") for j in site.get("jsonld") or [] if isinstance(j.get("name"), str)), "")
        if name:
            out["brand_name"] = {"value": name[:120], "basis": "stated", "page": site["url"]}
    return out


def _found_preview(fields: dict) -> list[str]:
    prods = (fields.get("key_products_services") or {}).get("value") or []
    bits = [str(p) for p in prods[:6]]
    for k in ("industry", "customer_location"):
        v = (fields.get(k) or {}).get("value")
        if isinstance(v, str) and v:
            bits.append(v)
    return bits


def _result(doc: dict) -> dict:
    return {"site": doc["site"], "url": doc["url"], "read_at": doc["fetched_at"],
            "fields": doc.get("fields") or {}, "pages": doc.get("pages") or []}


def _fresh(at: Optional[datetime]) -> bool:
    if not at:
        return False
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at < CACHE_FRESH


# ── comparing with the Playbook, and applying ──────────────────────────────────

def _labels() -> dict[str, tuple[str, str]]:
    from app.agents.social_media_manager.services.playbook_health import FIELDS as PLAYBOOK_FIELDS
    labels = {f["key"]: (f["label"], f["section"]) for f in PLAYBOOK_FIELDS}
    labels.update(_EXTRA_FIELDS)
    return labels


def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return " ".join(v.lower().split())
    if isinstance(v, list):
        return sorted(_norm(x) for x in v if x)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items() if x}
    return v


def _empty(v: Any) -> bool:
    return v in (None, "", [], {})


def compare(fields: dict, profile: dict) -> list[dict]:
    """Each finding next to what the Playbook has now. status: new (Playbook empty), same, or
    different. Only new findings start selected: nothing the owner wrote is replaced by default."""
    labels = _labels()
    rows = []
    for key, f in fields.items():
        current = profile.get(key)
        found = f["value"]
        if _empty(current):
            status = "new"
        elif _norm(current) == _norm(found):
            status = "same"
        elif isinstance(found, list) and isinstance(current, list) and set(_norm(found)) <= set(_norm(current)):
            status = "same"
        elif isinstance(found, dict) and isinstance(current, dict) and all(current.get(k) for k in found):
            status = "same"
        else:
            status = "different"
        label, section = labels.get(key, (key.replace("_", " ").capitalize(), "Brand Identity"))
        rows.append({"key": key, "label": label, "section": section, "found": found, "current": current,
                     "basis": f["basis"], "page": f.get("page", ""), "status": status,
                     "selected": status == "new",
                     "merge": isinstance(found, (list, dict))})
    order = ["Brand Identity", "Business Details", "Audience", "Brand Voice", "Brand Colors", "Language & Region"]
    rows.sort(key=lambda r: (order.index(r["section"]) if r["section"] in order else 99))
    return rows


def build_update(fields: dict, chosen: list[str], profile: dict, edits: Optional[dict] = None,
                 now: Optional[datetime] = None) -> dict:
    """The partial Playbook save for the chosen fields. Lists and social links are added to what
    the owner already has, never replacing it; text replaces. Each applied field is recorded in
    field_sources so the Playbook can show "From your website" until the owner changes it."""
    now = now or datetime.now(timezone.utc)
    edits = edits or {}
    data: dict[str, Any] = {}
    sources = dict(profile.get("field_sources") or {})
    for key in chosen:
        if key not in fields:
            continue
        value = edits.get(key, fields[key]["value"])
        current = profile.get(key)
        if isinstance(value, list):
            existing = [x for x in (current or []) if x] if isinstance(current, list) else []
            seen = {_norm(x) for x in existing}
            value = existing + [x for x in value if _norm(x) not in seen]
        elif isinstance(value, dict):
            value = {**{k: v for k, v in value.items() if v}, **{k: v for k, v in (current or {}).items() if v}} \
                if isinstance(current, dict) else value
        data[key] = value
        sources[key] = {"source": "you" if key in edits else "website",
                        "basis": "stated" if key in edits else fields[key]["basis"],
                        "page": fields[key].get("page", ""), "value": value, "at": now.isoformat()}
    if data:
        data["field_sources"] = sources
    return data


def source_of(profile: dict, key: str) -> str:
    """'website' or 'guess' while a field still holds what the website gave it; 'you' once the
    owner has changed it (or never took it from the website)."""
    s = (profile.get("field_sources") or {}).get(key)
    if not s or s.get("source") != "website":
        return "you"
    if _norm(s.get("value")) != _norm(profile.get(key)):
        return "you"
    return "guess" if s.get("basis") == "inferred" else "website"
