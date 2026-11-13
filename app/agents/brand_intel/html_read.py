"""
Pulls what matters out of one HTML page, with the standard library's parser (no new dependency).

Keeps: title and meta description, Open Graph tags, the theme colour, headings and visible text
(skipping script, style, nav-like noise), every link with its text, social profile links,
JSON-LD business data (name, address, price range, phone), stylesheet links and colours written
in the page, and logo candidates.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit

_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}
_BLOCK = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th",
          "br", "tr", "header", "footer", "main", "aside", "blockquote", "dd", "dt", "figcaption"}
_HEADINGS = {"h1", "h2", "h3"}
_SOCIAL = {
    "instagram": r"instagram\.com/(?!p/|reel/|explore/)([A-Za-z0-9_.]+)",
    "facebook": r"facebook\.com/(?!sharer|share|dialog|plugins)([A-Za-z0-9_.\-]+)",
    "tiktok": r"tiktok\.com/@([A-Za-z0-9_.]+)",
    "x": r"(?:twitter|x)\.com/(?!intent|share|home)([A-Za-z0-9_]+)",
    "linkedin": r"linkedin\.com/(?:company|in)/([A-Za-z0-9_\-]+)",
    "youtube": r"youtube\.com/(?:@|c/|channel/|user/)([A-Za-z0-9_\-]+)",
    "whatsapp": r"(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?(\+?\d{7,15})",
}
_HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
# A colour the site's own CSS names as its brand/primary/accent colour is the strongest signal.
_NAMED_BRAND = re.compile(r"--[\w-]*(?:primary|brand|accent|main|theme)[\w-]*\s*:\s*(#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))\b", re.I)
# CSS-framework defaults (Tailwind/Bootstrap status colours) say nothing about the brand.
_FRAMEWORK_DEFAULTS = {
    "#ef4444", "#dc2626", "#f87171", "#22c55e", "#16a34a", "#10b981", "#3b82f6", "#2563eb", "#60a5fa",
    "#eab308", "#f59e0b", "#fbbf24", "#e3f2fd", "#0d6efd", "#198754", "#dc3545", "#ffc107", "#0dcaf0",
    "#6366f1", "#8b5cf6", "#a855f7", "#ec4899", "#f97316", "#14b8a6", "#06b6d4", "#007bff", "#28a745",
}
# Pages that never describe the business.
_SKIP_PATHS = re.compile(r"/(terms|privacy|policy|policies|cookie|legal|login|log-in|signin|sign-in|signup|"
                         r"sign-up|register|account|cart|checkout|wp-admin|careers|jobs)\b", re.I)


@dataclass
class PageRead:
    url: str
    title: str = ""
    description: str = ""
    og: dict = field(default_factory=dict)
    theme_color: str = ""
    headings: list = field(default_factory=list)
    text: str = ""
    links: list = field(default_factory=list)        # [(absolute_url, link_text)]
    socials: dict = field(default_factory=dict)      # network -> profile URL
    jsonld: list = field(default_factory=list)       # business-shaped JSON-LD objects
    stylesheets: list = field(default_factory=list)
    colors: Counter = field(default_factory=Counter)
    logo_candidates: list = field(default_factory=list)
    lang: str = ""


def add_colors(counter: Counter, css: str) -> None:
    """Count the colours in some CSS; ones named as the brand/primary/accent colour count 50x."""
    counter.update(c.lower() for c in _HEX.findall(css))
    for c in _NAMED_BRAND.findall(css):
        counter[c.lower()] += 50


class _Parser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.r = PageRead(url=base)
        self._skip = 0
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self._chunks: list[str] = []
        self._heading: Optional[str] = None
        self._heading_buf: list[str] = []
        self._link: Optional[str] = None
        self._link_buf: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.r.lang = a["lang"][:10]
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_jsonld = True
            self._jsonld_buf = []
            return
        if tag == "style":
            self._in_style = True
        if tag in _SKIP:
            self._skip += 1
            return
        if a.get("style"):
            add_colors(self.r.colors, a["style"])
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "").strip()
            if name == "description" and content:
                self.r.description = content[:500]
            elif name.startswith("og:") and content:
                self.r.og[name[3:]] = content[:500]
            elif name == "theme-color" and content:
                self.r.theme_color = content[:20]
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if "stylesheet" in rel and href:
                self.r.stylesheets.append(urljoin(self.base, href))
            if "icon" in rel and href:
                self.r.logo_candidates.append(urljoin(self.base, href))
        elif tag == "a":
            href = a.get("href", "")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._link = urljoin(self.base, href)
                self._link_buf = []
        elif tag == "img":
            src = a.get("src") or a.get("data-src") or ""
            hint = (a.get("alt", "") + " " + a.get("class", "") + " " + src).lower()
            if src and "logo" in hint:
                self.r.logo_candidates.insert(0, urljoin(self.base, src))
        if tag in _HEADINGS:
            self._heading, self._heading_buf = tag, []
        if tag in _BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self._take_jsonld("".join(self._jsonld_buf))
            return
        if tag == "style":
            self._in_style = False
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link:
            self.r.links.append((self._link, " ".join("".join(self._link_buf).split())[:80]))
            self._link = None
        if tag in _HEADINGS and self._heading == tag:
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self.r.headings.append(text[:160])
            self._heading = None
        if tag in _BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_style:
            add_colors(self.r.colors, data)
            return
        if self._skip:
            return
        if self._in_title:
            self.r.title += data
            return
        self._chunks.append(data)
        if self._heading:
            self._heading_buf.append(data)
        if self._link:
            self._link_buf.append(data)

    def _take_jsonld(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(isinstance(k, str) and (k in ("Organization", "LocalBusiness", "Corporation", "Store",
                                                 "Restaurant", "ProfessionalService") or "Business" in k)
                   for k in kinds):
                keep = {k: item[k] for k in ("name", "description", "slogan", "address", "areaServed",
                                             "priceRange", "telephone", "sameAs", "logo", "url") if k in item}
                self.r.jsonld.append(keep)


def read_page(html: str, url: str) -> PageRead:
    p = _Parser(url)
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # a malformed page still gives whatever was parsed before the error
    r = p.r
    r.title = " ".join(r.title.split())[:200]
    lines = [" ".join(line.split()) for line in "".join(p._chunks).split("\n")]
    seen: set[str] = set()
    kept = []
    for line in lines:
        if len(line) < 3 or line.lower() in seen:
            continue
        seen.add(line.lower())
        kept.append(line)
    r.text = "\n".join(kept)
    for href, _ in r.links:
        for network, pattern in _SOCIAL.items():
            if network not in r.socials and re.search(pattern, href, re.I):
                r.socials[network] = href.split("?")[0] if network != "whatsapp" else href
    for item in r.jsonld:
        for same in item.get("sameAs") or []:
            for network, pattern in _SOCIAL.items():
                if network not in r.socials and isinstance(same, str) and re.search(pattern, same, re.I):
                    r.socials[network] = same
    if r.og.get("image"):
        r.logo_candidates.append(urljoin(url, r.og["image"]))
    return r


_PAGE_HINTS = [
    ("about", ("about", "who-we-are", "our-story", "company", "story")),
    ("services", ("service", "what-we-do", "solutions", "offer")),
    ("products", ("product", "shop", "store", "catalog", "collection", "menu")),
    ("pricing", ("pricing", "price", "plans", "packages", "rates")),
    ("contact", ("contact", "location", "find-us", "visit")),
    ("faq", ("faq", "questions", "help")),
    ("reviews", ("testimonial", "review", "clients", "case-stud")),
]


def pick_pages(home: PageRead, limit: int = 9) -> list[tuple[str, str]]:
    """The pages on the same site most likely to describe the business: [(kind, url)], best first."""
    home_host = (urlsplit(home.url).hostname or "").lower().removeprefix("www.")
    best: dict[str, tuple[int, str]] = {}
    for href, text in home.links:
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https"):
            continue
        if (parts.hostname or "").lower().removeprefix("www.") != home_host:
            continue
        path = (parts.path or "/").lower()
        if path in ("", "/") or re.search(r"\.(pdf|jpg|jpeg|png|gif|webp|zip|mp4|svg)$", path):
            continue
        if _SKIP_PATHS.search(path):
            continue
        hay = f"{path} {text.lower()}"
        for rank, (kind, words) in enumerate(_PAGE_HINTS):
            if any(w in hay for w in words):
                clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
                score = rank * 10 + path.count("/")
                if kind not in best or score < best[kind][0]:
                    best[kind] = (score, clean)
                break
    ordered = sorted(best.items(), key=lambda kv: kv[1][0])
    return [(kind, url) for kind, (_, url) in ordered][:limit]


def top_colors(counter: Counter, theme: str = "", limit: int = 3) -> list[str]:
    """The brand's likely colours: the theme colour first, then the most-used non-grey colours."""
    out: list[str] = []
    if theme and _HEX.fullmatch(theme.strip()):
        out.append(_expand(theme.strip().lower()))
    for color, _ in counter.most_common(60):
        c = _expand(color)
        if c in out or _greyish(c) or c in _FRAMEWORK_DEFAULTS:
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _expand(c: str) -> str:
    c = c.lower()
    return "#" + "".join(ch * 2 for ch in c[1:]) if len(c) == 4 else c


def _greyish(c: str) -> bool:
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    return max(r, g, b) - min(r, g, b) < 24
