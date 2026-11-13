"""
Finding a business's competitors (PRD Workstream 2), from its Brand Playbook.

One model call with web search names candidates; every candidate is then CHECKED against a real
page before it is shown, because a model asked for "businesses like this in Lekki" will
otherwise invent plausible names. A candidate survives only if its website, or the page the
search found it on, loads and actually mentions it. Owners see only businesses that exist.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

import openai

from app.agents.brand_intel.html_read import read_page
from app.agents.brand_intel.safe_fetch import FetchError, fetch, site_key
from app.core.config import settings

MODELS = ("gpt-5.5", "gpt-5.4", "gpt-4o")
SEARCH_TOOLS = ({"type": "web_search"}, {"type": "web_search_preview"})
MAX_CANDIDATES = 10


class DiscoveryUnavailable(Exception):
    pass


def brief_from_profile(profile: dict) -> dict:
    """The parts of the Playbook that decide who a competitor is."""
    def as_list(v):
        return [str(x).strip() for x in (v or []) if str(x).strip()] if isinstance(v, list) else (
            [s.strip() for s in str(v or "").split(",") if s.strip()])
    return {
        "name": (profile.get("brand_name") or "").strip(),
        "industry": (profile.get("industry") or "").strip(),
        "what_they_do": (profile.get("product_description") or "").strip(),
        "products": as_list(profile.get("key_products_services"))[:8],
        "customers": (profile.get("ideal_customer_profile") or "").strip(),
        "area": (profile.get("customer_location") or profile.get("region") or "").strip(),
        "website": (profile.get("website") or "").strip(),
    }


def enough_to_search(brief: dict) -> bool:
    return bool(brief["industry"] or brief["what_they_do"] or brief["products"]) and bool(brief["area"])


def _prompt(brief: dict, exclude: list[str]) -> str:
    lines = [f"- {k.replace('_', ' ')}: {', '.join(v) if isinstance(v, list) else v}"
             for k, v in brief.items() if v]
    skip = f"\nDo not include these (the owner already ruled them out or has them): {', '.join(exclude[:30])}." if exclude else ""
    return (
        "Find this business's real competitors by searching the web.\n"
        + "\n".join(lines)
        + "\n\nList up to 8 real businesses customers would compare it with: first ones that sell the same "
          "thing to the same customers in the same area (kind \"direct\"), then ones that meet the same need "
          "another way or elsewhere (kind \"similar\"). Only businesses you found on the web, with a page that "
          "shows they exist. Never this business itself." + skip + "\n\n"
        'Return ONLY JSON: {"competitors": [{"name": "...", "website": "https://... or empty", '
        '"instagram": "handle or empty", "kind": "direct"|"similar", "location": "...", '
        '"what_they_sell": "a few words", "why": "one sentence on why customers would compare them", '
        '"source_url": "the page that shows this business exists"}]}'
    )


def _json_from(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


async def search(brief: dict, exclude: list[str]) -> list[dict]:
    """Candidates from one web-searching model call. Raises DiscoveryUnavailable."""
    if not settings.OPENAI_API_KEY:
        raise DiscoveryUnavailable("OPENAI_API_KEY is not configured")
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    last: Optional[Exception] = None
    for model in MODELS:
        for tool in SEARCH_TOOLS:
            try:
                resp = await client.responses.create(
                    model=model, tools=[tool], input=_prompt(brief, exclude), timeout=120)
                data = _json_from(getattr(resp, "output_text", "") or "")
                return [c for c in (data.get("competitors") or []) if isinstance(c, dict)][:MAX_CANDIDATES]
            except (openai.NotFoundError, openai.PermissionDeniedError) as e:
                last = e
                break  # this model isn't available here: next model
            except openai.BadRequestError as e:
                last = e
                if "model" in str(e).lower():
                    break
                continue  # this search tool name isn't supported: try the other
            except Exception as e:
                if "insufficient_quota" in str(e):
                    from app.services.AIService import _alert_openai_out_of_credit
                    await _alert_openai_out_of_credit(str(e))
                raise DiscoveryUnavailable(str(e))
    raise DiscoveryUnavailable(str(last))


def _clean_handle(h: str) -> str:
    h = (h or "").strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", h)
    h = m.group(1) if m else h
    return re.sub(r"[^A-Za-z0-9_.]", "", h.lstrip("@"))[:30]


def _name_tokens(name: str) -> list[str]:
    stop = {"the", "and", "ltd", "limited", "nigeria", "lagos", "company", "co", "inc", "services", "enterprises"}
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 2 and t not in stop]


def _mentions(text: str, name: str) -> bool:
    tokens = _name_tokens(name)
    if not tokens:
        return False
    low = text.lower()
    return sum(1 for t in tokens if t in low) >= max(1, (len(tokens) + 1) // 2)


async def _check(candidate: dict) -> Optional[dict]:
    """The candidate, tidied, if a real page shows it exists; otherwise None."""
    name = " ".join(str(candidate.get("name") or "").split())[:80]
    if not name:
        return None
    website = str(candidate.get("website") or "").strip()
    source = str(candidate.get("source_url") or "").strip()
    out = {
        "name": name,
        "website": "",
        "instagram": _clean_handle(str(candidate.get("instagram") or "")),
        "kind": "direct" if candidate.get("kind") == "direct" else "similar",
        "location": " ".join(str(candidate.get("location") or "").split())[:80],
        "what_they_sell": " ".join(str(candidate.get("what_they_sell") or "").split())[:120],
        "why": " ".join(str(candidate.get("why") or "").split())[:240],
        "source_url": "",
        "description": "",
    }
    for url, is_site in ((website, True), (source, False)):
        if not url:
            continue
        try:
            page = await fetch(url, max_bytes=400_000)
        except FetchError:
            continue
        read = read_page(page.text, page.url)
        if _mentions(f"{read.title} {read.description} {read.text[:20000]}", name):
            if is_site:
                out["website"] = page.url
                out["description"] = read.description[:240]
                if not out["instagram"] and read.socials.get("instagram"):
                    out["instagram"] = _clean_handle(read.socials["instagram"])
            out["source_url"] = page.url
            return out
    return None


async def verify(candidates: list[dict], own_site: str = "", own_name: str = "") -> list[dict]:
    """Keep only candidates a real page vouches for; never the business itself; no duplicates."""
    checked = await asyncio.gather(*(_check(c) for c in candidates))
    own_key = site_key(own_site) if own_site else ""
    own_tokens = set(_name_tokens(own_name))
    seen: set[str] = set()
    out = []
    for c in checked:
        if not c:
            continue
        key = site_key(c["website"]) if c["website"] else c["name"].lower()
        if key in seen or (own_key and c["website"] and site_key(c["website"]) == own_key):
            continue
        if own_tokens and set(_name_tokens(c["name"])) == own_tokens:
            continue
        seen.add(key)
        out.append(c)
    out.sort(key=lambda c: 0 if c["kind"] == "direct" else 1)
    return out


def cache_key(brief: dict) -> str:
    """Brands with the same industry, products and area get the same search."""
    norm = lambda s: " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))
    products = ",".join(sorted(norm(p) for p in brief["products"]))[:300]
    return "|".join([norm(brief["industry"]), norm(brief["what_they_do"])[:200], products, norm(brief["area"])])


def candidate_key(c: dict) -> str:
    return site_key(c["website"]) if c.get("website") else re.sub(r"[^a-z0-9]", "", c["name"].lower())


def handle_for_playbook(c: dict) -> str:
    """How a kept competitor is written into the Playbook's competitor list."""
    return f"@{c['instagram']}" if c.get("instagram") else c["name"]


def summary(c: dict) -> dict[str, Any]:
    return {k: c.get(k, "") for k in ("name", "website", "instagram", "kind", "location", "what_they_sell",
                                      "why", "source_url", "description")}
