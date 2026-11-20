"""
VSG-01 v3 — the format-selection + generation orchestrator (§6-9, step 10).

Every format module and every supporting primitive (brand_tokens, visual_slots,
legibility, skin_tone_check, isolation_cap, attribute_tagging) was built and
shipped with the same note in its own docstring: "not yet wired into a live
call path... this is the primitive that step calls." This module is that
step — the one place that actually calls them together, in order, around a
real render.

SCOPE. All 15 formats are real, retrievable corpus records (step 9) and
`select_ranked_ad_formats` below applies the full VSG-01 eligibility logic to
all of them via the SAME retrieval engine every other corpus category already
uses (retrieval.py's exclusion_reason/retrieve — nothing format-specific was
added there). Seven formats are wired to actually GENERATE a creative from
this module:

  No real photo needed (GENERATE path, `NO_PHOTO_FORMAT_IDS`):
    SEED-075 Us vs Them, SEED-087 Borrowed Interface, SEED-080 Problem/Solution,
    SEED-081 Receipt (content-fill only succeeds when the business's own words
    already state real, verbatim prices — see `_content_receipt`; otherwise
    this format simply never has content to render, same fail-open contract
    as every builder here)

  Needs a real, attested photo (UPLOAD/RECOMPOSITE paths, `UPLOAD_PHOTO_FORMAT_IDS`
  / `RECOMPOSITE_PHOTO_FORMAT_IDS`):
    SEED-093 Review Card, SEED-082 Text on a Face, SEED-074 Testimonial+Offer
    (person path), SEED-088 Starter Pack (recomposite only — see below)

`select_and_render_vsg01_creative` tries every eligible, ranked candidate in
order (not just the top one) — a format whose real-content step comes up
empty (no genuine quote/price stated) falls through to the next-ranked
format before ever falling all the way back to the generic single image.

Why Starter Pack is recomposite-only, not upload: it composites the client's
product into a grid *next to* separately generated flat-lay items
(`ad_formats/starter_pack.py`) — that only reads right against a clean cutout,
which `creative_from_recomposite`'s existing background-removal pipeline
already produces (`ImageContentService._generate_platform_image(reference_image=...)`,
proven for RECOMPOSITE); a raw, uncropped upload would composite as a visible
rectangle. Review Card / Text on a Face / Testimonial+Offer composite the
photo as one full-bleed zone, not next to generated items, so a raw upload
looks fine there regardless.

Quotes and prices are never invented. `_content_review_card`,
`_content_testimonial_offer`, and `_content_receipt` all extract from the
caller's own `description` text (LLM call instructed to return null/empty
unless the text states something verbatim) rather than composing one —
fabricating a customer's words or a business's prices is exactly what
§2.1/§2.2/§2.5 forbid. Known limitation, not a bug: in the chat-based launch
flow, `description` has already passed through nl.py's own NL-parsing step
before it reaches here, so a quote/price mentioned earlier in a longer
conversation may not have survived into it — these three formats will
under-fire rather than over-fire, which is the safe direction to fail in.

News Headline (SEED-077) and Humour/Cartoon (SEED-089) now have real
builders (`_build_news_headline`, `_build_humour_cartoon`) registered in
`_BUILDERS` — the full content-generation + Layer 2 + build_document path
is genuine, tested code, not a stub. Both are still deliberately NEVER
auto-selected in the live `select_and_render_vsg01_creative` path, for two
different real reasons, not a gap in this module:
  - News Headline requires `isolated_ad_account=True` (its corpus record's
    `pooled_account_safe=REQUIRES_ISOLATION`, enforced by retrieval.py's own
    exclusion_reason). Grep confirms no per-brand ad account exists anywhere
    in this codebase — every brand advertises through the single global
    `settings.META_AD_ACCOUNT_ID`. This is a platform-level gap (per-brand
    Meta ad accounts, a separate infrastructure initiative), not something a
    BusinessProfile flag here can honestly satisfy. Membership in
    `NO_PHOTO_FORMAT_IDS` is future-readiness, not a live path.
  - Humour/Cartoon's own builder hard-requires `human_reviewed=True` with no
    default, and the automatic call path never passes it — satisfying §2.12
    honestly means an async generate-hold-approve-resume workflow this
    endpoint doesn't have, not a data flag pretending to be one. Both
    builders are directly callable (with `human_reviewed=True` /
    `isolated_ad_account` bypassed) for manual/QA rendering outside the
    retrieval gate — see `/jane-ads/debug/vsg01-format-direct` in router.py.

The Censored Item (SEED-083) and Day 1 -> Day 30 (SEED-078) now also have
real builders (`_build_censored_item`, `_build_day1_day30`), completing all
15 formats' registration in `_BUILDERS`. Both are `upload`-only (never
generate the real product/progress photo itself — §1.2) and both are also
`pooled_account_safe=REQUIRES_ISOLATION`, the same platform-level gap as
News Headline above — neither auto-fires live today, for the same reason.
Day 1 -> Day 30 has a SECOND real blocker on top: it needs two genuine
photos of the same thing at two points in time (`day30_photo_url`), and no
attestation flow in this codebase produces a second photo today (only
single product_photo/real_customer_photo attestations exist) — so even
with isolation lifted, an automatic call (which only ever has one
`photo_url`) would still resolve to None via its own builder. Both
builders are directly callable via `/jane-ads/debug/vsg01-format-direct`
for manual/QA rendering, same as News Headline/Humour-Cartoon above.

Falls back to the existing generic image (`generate_ad_image`) at every
possible failure point — selection returning nothing, every candidate's
content step coming up empty, a built document failing its own legibility
check, a generated scene failing its skin-tone check, or the render call
itself failing. Never raises; `select_and_render_vsg01_creative` returns None
on any of these, exactly like `generate_ad_image` already does.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Optional

from app.core.config import settings

from .ad_formats import (
    borrowed_interface, problem_solution, review_card, starter_pack,
    testimonial_offer, text_on_a_face, us_vs_them,
)
from .ad_formats.attribute_tagging import build_ad_format_attributes
from .ad_formats.brand_tokens import resolve_brand_tokens
from .ad_formats.legibility import check_legibility
from .entities import ConsumedBy, Strategy, StrategyCategory, StrategyPlatform
from .layer2_generation import SceneGenerationFailed, generate_scene
from .retrieval import BudgetContext, BusinessProfile, RetrievalRequest, retrieve
from .skin_tone_check import verify_skin_rendering
from .store import MongoStrategyStore
from .visual_slots import NIGERIAN_SETTINGS
from .vsg01_corpus_seed import _RECORDS as _VSG01_RECORDS
from .vsg01_corpus_seed import FORMAT_MODULES

# Each format's own real corpus claim (vsg01_corpus_seed.py's own authored
# "use when..." sentence) — the actual content this format is for, used by
# _content_fit_boost below to REASON about a fit rather than pattern-match
# a hardcoded phrase list.
_FORMAT_CLAIMS: dict[str, str] = {
    r["format_module"].FORMAT.format_id: r["claim"] for r in _VSG01_RECORDS
}

# News Headline/Censored Item/Day 1->Day 30's corpus records are
# pooled_account_safe=REQUIRES_ISOLATION, and retrieval.py's exclusion_reason
# only ever passes that when profile.isolated_ad_account is True. No
# per-brand Meta ad account exists anywhere on this platform (grep-confirmed:
# every launch/billing/monitoring call uses the single global
# settings.META_AD_ACCOUNT_ID) — but §6/§2.8's own design already names the
# real compensating control for exactly this situation: "cap usage across the
# book before scaling" (SEED-079, isolation_cap.py) — a book-wide usage
# ceiling built specifically so these formats can run safely on a POOLED
# account, not a literal separate one. That module existed, fully built and
# tested, with a real docstring saying "not yet wired into a live call path"
# — select_and_render_vsg01_creative's own render loop is that wiring: it
# enforces the actual cap (check before rendering, record after) per
# candidate. This constant is what makes isolated_ad_account=True a TRUE
# statement wherever it's used — "safe on this pooled account under the real
# cap enforced elsewhere", not a false claim about account architecture —
# shared so every call site that ranks VSG01 formats (the real generation
# path AND the pre-generation suggest-format endpoint) applies the exact
# same eligibility, rather than two hand-maintained copies that could drift
# apart (confirmed they already had: suggest-format never set this at all,
# so these 3 formats could never even appear as a suggestion).
VSG01_ISOLATED_AD_ACCOUNT = True

# See module docstring for the full reasoning behind each set below.
NO_PHOTO_FORMAT_IDS = frozenset({
    "SEED-075", "SEED-087", "SEED-080", "SEED-081",
    "SEED-097",
    # Work In Progress ideally prefers a real upload of the client's own work
    # (VSG-01-PROMPTS v2 §6.15) — wired here on the generate path only, since
    # no photo-attestation type exists yet for "this is real work in
    # progress" (only product_photo/real_customer_photo do). See
    # work_in_progress.py's own module docstring for this disclosed gap.
    "SEED-098",
    # News Headline — included for completeness/readiness, but retrieval.py's
    # own exclusion_reason (pooled_account_safe=REQUIRES_ISOLATION) blocks it
    # from ever actually being selected until isolated_ad_account=True is
    # genuinely true for a request, which nothing in this codebase sets
    # today (see module docstring). Membership here has no live effect until
    # that infrastructure exists.
    "SEED-077",
    # Humour/Cartoon — included for completeness/readiness, but its own
    # builder (_build_humour_cartoon) hard-requires human_reviewed=True,
    # which the automatic call path here never passes — so it always
    # resolves to None and falls through, never actually auto-firing without
    # real human review (see that builder's own docstring).
    "SEED-089",
    # Poster formats. Event Promo without a photo is a type-and-date poster; Offer Spotlight
    # generates a situation photo; Problems vs Solution without a product lists outcomes.
    "SEED-099", "SEED-100", "SEED-101",
    # Second poster set: three generate a situation photo, the billboard generates a blank
    # billboard (our renderer sets the words), AI Answer is drawn.
    "SEED-102", "SEED-103", "SEED-104", "SEED-105", "SEED-106",
})
UPLOAD_PHOTO_FORMAT_IDS = frozenset({
    # Event Promo with the host's own photo; Problems vs Solution with the real product;
    # Portrait + Headline with the owner's own photo.
    "SEED-099", "SEED-101", "SEED-102",
    "SEED-093", "SEED-082", "SEED-074", "SEED-096",
    # Censored Item / Day 1 -> Day 30 — same completeness/readiness note as
    # News Headline above: both are also pooled_account_safe=REQUIRES_ISOLATION,
    # so retrieval.py blocks them from live selection regardless of
    # membership here. Day 1 -> Day 30 additionally needs a SECOND real photo
    # (day30_photo_url) that no attestation flow produces today, so even a
    # single-photo_url automatic call would still resolve to None via its own
    # builder — membership here has no live effect until both gaps close.
    "SEED-083", "SEED-078",
})
RECOMPOSITE_PHOTO_FORMAT_IDS = UPLOAD_PHOTO_FORMAT_IDS | {"SEED-088"}

# The format library's own tested/documented canvas — every format module's
# unit tests and hard-check reasoning (line wrapping, scrim heights, column
# widths) assume this square shape. Ad placements elsewhere in this codebase
# are 9:16 vertical (creative.py's _AD_IMAGE_PLATFORM/_AD_IMAGE_TYPE) — moving
# these formats to a vertical canvas is real future work needing a per-format
# layout review (anchoring, empty space below shorter content), not a
# same-day resize.
_CANVAS_SIZE = (1080, 1080)


# ── Content fit (Layer 2) ─────────────────────────────────────────────────
# Eligibility (retrieval.py, above) answers "what CAN this business run" —
# photo/budget/platform facts about the account. It has no idea what the ad
# actually SAYS. This layer narrows among the already-eligible formats
# based on what `description` is actually saying — it never makes an
# ineligible format eligible, and "nothing stands out" is never penalized,
# just left in its original (eligibility-score) order.
#
# _content_fit_boost (below _CONTENT_FIT_SIGNALS) is the real, primary
# mechanism: one content-model call reasoning about the brief against each
# ELIGIBLE format's own authored corpus claim — genuinely understands a
# differently-worded announcement/reveal/complaint, not just phrases
# someone thought to enumerate in advance. _CONTENT_FIT_SIGNALS below is
# kept as _keyword_content_fit_boost, a zero-latency, zero-cost fallback
# for when that call fails (an LLM outage should degrade the suggestion
# quality, not remove content-fit entirely) — never the primary path, and
# itself a real, live-confirmed illustration of why: it originally had no
# entry at all for News Headline, so a brief that was EXACTLY a news
# announcement ("we're opening a new branch in Yaba on 1 October, tell
# people about it") still fell through to Problem/Solution's default.
_CONTENT_FIT_SIGNALS: dict[str, tuple[str, ...]] = {
    "SEED-099": (
        "webinar", "workshop", "masterclass", "master class", "class on", "challenge", "bootcamp",
        "training", "seminar", "register", "sign up", "join us on", "live session", "meetup",
    ),
    "SEED-100": (
        "% off", "percent off", "discount", "promo", "sale", "anniversary", "deal", "slashed",
        "half price", "buy one", "free delivery",
    ),
    "SEED-101": (
        "tired of", "struggling", "frustrat", "stress of", "no more", "problem", "hassle",
    ),
    "SEED-102": (
        "fresh", "farm", "made by", "handmade", "meet our", "our team", "we deliver", "home-grown",
    ),
    "SEED-103": (
        "app", "dashboard", "software", "track your", "inventory", "manage your", "platform", "automat",
    ),
    "SEED-104": (
        "enrol", "enroll", "admission", "apply now", "applications", "cohort", "intake", "sign up",
        "register now", "membership", "join our", "secure your seat", "course",
    ),
    "SEED-105": (
        "bold statement", "truth", "myth", "is not", "isn't", "stop thinking", "billboard", "mindset",
    ),
    "SEED-106": (
        "ask ai", "chatgpt", "people ask", "how do i", "what should i", "question", "advice", "guide",
    ),
    # Us vs Them — the brief itself frames a comparison.
    "SEED-075": (
        "vs ", "vs.", "versus", "unlike other", "unlike most", "compared to",
        "better than", "while other", "other salons", "other brands",
        "other shops", "not like other",
    ),
    # Price-Led Offer — a concrete price, discount, or markdown is stated.
    # UPLOAD_PHOTO_FORMAT_IDS only (needs a real product photo to composite
    # the price onto) — see module docstring's Precedence note: this signal
    # can only ever win when a photo was actually provided.
    "SEED-096": (
        "% off", "percent off", "discount", "was ₦", "was $", "was £",
        "now ₦", "now $", "now only", "limited time price", "price drop",
        "slash", "half price",
    ),
    # Text-Only — the NO_PHOTO-eligible home for a stated price/discount/offer
    # when there's no product photo. NOT Receipt: a receipt is an itemised
    # breakdown ("haircut ₦3,000, wash ₦1,000"), and `_content_receipt`
    # correctly rejects a single before/after price, so pointing this signal
    # at Receipt just guaranteed a fall-through. Text-Only's own corpus
    # record is exactly this case: "one line of real information — a price, a
    # delivery area, a specific offer". Once a photo is attached, candidate_ids
    # moves to UPLOAD_PHOTO_FORMAT_IDS and SEED-096 above takes over instead.
    "SEED-097": (
        "% off", "percent off", "discount", "was ₦", "was $", "was £",
        "now ₦", "now $", "now only", "limited time price", "price drop",
        "slash", "half price",
        # kept from the original Text-Only signal set
        "just says", "one line", "bold statement", "headline that reads",
    ),
    # Testimonial+Offer / Review Card — a real customer's own words are quoted.
    "SEED-074": (
        "customer said", "she said", "he said", "they said", '"',
        "testimonial", "highly recommend", "loved it", "changed my",
    ),
    "SEED-093": ("review", "star rating", "rated us", "5 stars", "customer said"),
    # Problem/Solution — the brief names a pain point before the fix.
    "SEED-080": (
        "tired of", "struggling with", "frustrat", "no more", "fix your",
        "sick of", "fed up",
    ),
    # Work In Progress — the brief is about an unfinished process, not a result.
    "SEED-098": (
        "coming soon", "in progress", "under construction", "still building",
        "work in progress", "sneak peek", "underway",
    ),
    # Starter Pack — the brief describes a bundle/kit, not a single item.
    "SEED-088": (
        "starter kit", "starter pack", "bundle", "set includes",
        "everything you need", "comes with",
    ),
    # News Headline — the brief states a genuine, dated announcement. Live-
    # confirmed gap: this format had NO signal entry at all, so a brief that
    # was exactly this shape ("we're opening a new branch in Yaba on 1
    # October, tell people about it") still fell through to Problem/
    # Solution's default — being eligible was never enough on its own to
    # ever get suggested.
    "SEED-077": (
        "we're opening", "now open", "opening on", "opens on", "launching on",
        "launch date", "new branch", "grand opening", "admissions close",
        "admissions open", "tell people about it", "announce", "announcing",
        "new campus", "now enrolling",
    ),
    # Humour/Cartoon — the brief frames a shared, relatable annoyance as the
    # ad's own angle (not just naming a problem to solve, which is
    # Problem/Solution's signal — "so annoying"/"let's be honest" read as
    # wanting to be funny about it, not just fix it).
    "SEED-089": (
        "so annoying", "the worst part", "we've all been there", "let's be honest",
        "nobody likes", "relatable", "make you laugh", "funny",
    ),
    # The Censored Item — a real pending reveal, distinct from Work In
    # Progress's "still building" framing: something specific is being kept
    # back until a stated moment.
    "SEED-083": (
        "revealing", "reveal on", "unveiling", "under wraps", "big reveal",
        "can't show you yet", "keeping it secret",
    ),
}


def _keyword_content_fit_boost(description: str) -> dict[str, float]:
    """The original mechanism — returns {format_id: 1.0} for every format
    whose signal phrases appear verbatim in the business's own stated
    brief. Real, live-confirmed limitation: a brief that says exactly what
    a format is for, just in different words than whatever was hardcoded
    here, gets no boost at all — "we're opening a new branch in Yaba on 1
    October, tell people about it" matched none of News Headline's
    original phrase list despite being exactly a news announcement. Kept
    now only as a zero-latency, zero-cost FALLBACK for when the real
    classifier below (_content_fit_boost) can't run — never the primary
    mechanism."""
    text = (description or "").lower()
    return {fid: 1.0 for fid, phrases in _CONTENT_FIT_SIGNALS.items() if any(p in text for p in phrases)}


# Styles that may only state facts the brief itself gives (they never invent a date, offer, price or
# quote), each with the fact it needs and how that's said to the client.
FACT_NEEDS: dict[str, tuple[str, str]] = {
    "SEED-099": ("event_with_date", "Needs an event with a date in your brief"),
    "SEED-100": ("offer", "Needs a real offer or discount in your brief"),
    "SEED-104": ("signup", "Needs a course, intake or registration in your brief"),
    "SEED-096": ("price", "Needs a price in your brief"),
    "SEED-081": ("item_prices", "Needs items with their prices in your brief"),
    "SEED-093": ("customer_quote", "Needs a real customer quote in your brief"),
    "SEED-074": ("customer_quote", "Needs a real customer quote in your brief"),
}


async def brief_unmet_needs(description: str) -> dict[str, str]:
    """{format_id: reason} for the fact-led styles this brief can't support, asked BEFORE generation
    so the Style step can say so up front (each style's own content step refuses the same briefs,
    and the client only found out afterwards: "Couldn't build Event Promo for this one").
    One content-model call; any failure returns {} so nothing is ever blocked by an outage."""
    text = (description or "").strip()
    if not text:
        return {fid: reason for fid, (_, reason) in FACT_NEEDS.items()}
    d = await _call_content_model(
        f"A business wrote this ad brief:\n\n{text}\n\n"
        "Answer ONLY from what the text actually states (never infer or assume):\n"
        "- event_with_date: does it announce a real upcoming event (webinar, class, launch, meetup...) "
        "WITH a stated date?\n"
        "- offer: does it state a real offer (a discount, a price cut, a free extra, a bundle)?\n"
        "- signup: is it about enrolling or registering (a course, admission, intake, cohort, membership)?\n"
        "- price: does it state a specific price?\n"
        "- item_prices: does it list items each with its own price or cost?\n"
        "- customer_quote: does it contain an actual customer quote or review?\n"
        'Return JSON: {"event_with_date": true|false, "offer": ..., "signup": ..., "price": ..., '
        '"item_prices": ..., "customer_quote": ...}. Return ONLY the JSON.'
    )
    if not isinstance(d, dict):
        return {}
    return {fid: reason for fid, (fact, reason) in FACT_NEEDS.items() if d.get(fact) is False}


async def _content_fit_boost(description: str, eligible: list[Strategy]) -> dict[str, float]:
    """Real content-fit classification: asks the content model which ONE
    eligible format's actual, authored corpus claim best matches what the
    business brief is really saying — an announcement, a reveal, a
    relatable complaint, a comparison, a bundle, a testimonial, an
    unfinished process — rather than pattern-matching a hardcoded phrase
    list that can only ever cover phrasings someone thought to enumerate.
    Only ever reasons about formats retrieval.py has ALREADY found
    eligible for this request (passed in as `eligible`) — this can narrow
    among them, never make an ineligible one eligible, same contract the
    keyword version always had.

    Empty/no-match description, an empty eligible list, or the content
    model call failing all return {} (no boost, no reorder) — the safe
    default, same as the keyword version — but on a genuine call failure
    this falls back to the keyword matcher above rather than going
    straight to the no-signal default order, so a transient LLM outage
    degrades to the old behaviour instead of losing content-fit entirely.
    """
    text = (description or "").strip()
    if not text or not eligible:
        return {}
    catalogue = "\n".join(
        f"- {s.strategy_id}: {_FORMAT_CLAIMS[s.strategy_id]}"
        for s in eligible if s.strategy_id in _FORMAT_CLAIMS
    )
    if not catalogue:
        return {}
    prompt = (
        f"A Nigerian business wrote this ad brief:\n\n{text}\n\n"
        "Below is a list of ad visual formats this business is ALREADY eligible to "
        "use, each with its own real, authored description of what it's actually "
        "for. Pick the ONE format whose real purpose best matches what this brief "
        "is genuinely saying — judge the actual content/situation being described, "
        "not just general ad quality. If nothing genuinely stands out over a plain "
        "default, return format_id as an empty string; do not force a fit.\n\n"
        f"{catalogue}\n\n"
        "Return JSON: {\"format_id\": \"SEED-XXX\" or \"\"}. Return ONLY the JSON."
    )
    try:
        d = await _call_content_model(prompt)
    except Exception as e:
        print(f"[VSG01] content-fit classification failed, falling back to keyword match: {e}", flush=True)
        return _keyword_content_fit_boost(description)
    if not d:
        return _keyword_content_fit_boost(description)
    picked = str(d.get("format_id", "")).strip()
    valid_ids = {s.strategy_id for s in eligible}
    return {picked: 1.0} if picked in valid_ids else {}


# The "no content signal fired" fallback order — an ORDERED list, tried in
# sequence, all hoisted above the plain eligibility ranking.
#
#  1. SEED-080 Problem/Solution — the corpus's OWN authored default:
#     "Default choice when nothing more specific fits — lowest policy risk in
#     the library... any business, especially where no more specific format
#     applies." Preferred when it renders (it generates a situational scene,
#     which can fail its §1.7 skin-tone check with no retry — confirmed live).
#  2. SEED-097 Text-Only — the reliable floor beneath it: no generated
#     imagery, no skin-tone gate, no verbatim-content parsing, just "one line
#     of real information set large on a plain field." Its corpus record is
#     explicitly "use when the business has no usable photograph at all."
#
# Without this, "nothing matched" fell through to retrieval.py's score()
# (grade x transfer x recency x origin — corpus data freshness, not fit) and
# in practice always landed on Us vs Them, the one no-photo format whose
# content step never fails. Ranked #1 != rendered: a higher pick whose
# content/build step comes up empty is skipped, so Text-Only as #2 here is
# what actually stops the Us-vs-Them-by-attrition problem.
_DEFAULT_FORMAT_IDS = ("SEED-080", "SEED-097")


async def select_ranked_ad_formats(
    db, *,
    has_product_photo: bool = False,
    has_real_customer_photo: bool = False,
    isolated_ad_account: bool = False,
    candidate_ids: Optional[frozenset] = None,
    description: str = "",
) -> list[Strategy]:
    """The actual §6 retrieval-time gate, applied to the CREATIVE_FORMATS
    category specifically. Reuses retrieval.py's exclusion_reason/retrieve
    unchanged — a format corpus record is excluded/scored exactly like every
    other category's record; nothing here is format-specific logic
    duplicating what retrieval.py already does.

    `candidate_ids` restricts the pool BEFORE retrieval (not a fake
    exclusion reason) — a call site passes exactly the formats this module
    can honestly finish given what it was called with (see the three
    *_FORMAT_IDS constants), so a corpus record this module can't build never
    gets selected in the first place, whatever its score. Returns every
    eligible Strategy, ranked — empty if nothing in the pool is eligible.
    `select_and_render_vsg01_creative` tries them in order, not just the
    top one, so one candidate's content step coming up empty doesn't fall
    all the way back to the generic image while a lower-ranked real format
    could still have worked.

    `description` — the business's own ad brief — applies Layer 2 content-fit
    on top of the eligibility ranking above (see `_content_fit_boost`): any
    eligible format whose signal phrases appear in the brief is moved ahead
    of eligible formats with no match, preserving the eligibility order
    within each group (stable sort). Empty/no-match `description` leaves the
    eligibility order exactly as retrieval.py produced it."""
    if db is None:
        return []
    approved = await MongoStrategyStore(db).fetch_approved()
    candidates = [
        s for s in approved
        if s.category is StrategyCategory.CREATIVE_FORMATS
        and (candidate_ids is None or s.strategy_id in candidate_ids)
    ]
    if not candidates:
        return []

    profile = BusinessProfile(
        has_product_photo=has_product_photo,
        has_real_customer_photo=has_real_customer_photo,
        isolated_ad_account=isolated_ad_account,
    )
    # Every VSG-01 format record ships budget_floor_ngn_daily=0.0 (see
    # vsg01_corpus_seed's own docstring) — a creative format has no inherent
    # minimum spend the way a budget-tactic record does. This BudgetContext
    # exists only to satisfy RetrievalRequest's shape; no real spend figure
    # can ever exclude a format record regardless of what's passed here.
    budget = BudgetContext(daily_spend_ngn=1_000_000.0, budget_tier=4)
    req = RetrievalRequest(
        stage=ConsumedBy.CREATIVE_BRIEF,
        platforms=[StrategyPlatform.META],
        budget=budget,
        profile=profile,
    )
    result = retrieve(candidates, req, limit=len(candidates))
    records = result.records
    fit = await _content_fit_boost(description, records)
    if fit:
        records = sorted(records, key=lambda s: -fit.get(s.strategy_id, 0.0))
    else:
        # No specific content signal fired — hoist the ordered fallback list
        # (see _DEFAULT_FORMAT_IDS) above the plain data-freshness score, in
        # its own order. Any id not eligible for this request is simply
        # absent and skipped (e.g. the upload-photo path never has these as
        # candidates) — falls through to plain eligibility order.
        rank = {fid: i for i, fid in enumerate(_DEFAULT_FORMAT_IDS)}
        records = sorted(records, key=lambda s: rank.get(s.strategy_id, len(rank)))
    return records


async def _call_content_model(prompt: str) -> Optional[dict]:
    """Same shape as creative.py's _call_ad_copy_model — a dedicated copy
    here rather than a cross-module private import, since this module's
    prompts return a different JSON shape per format, not an AdCopy."""
    if not settings.jane_ads_openai_key:
        return None
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[VSG01] content model error: {e}", flush=True)
        # Own independent OpenAI client (jane_ads_openai_key) — never routed
        # through AIService.py's out-of-credit alert like every other caller.
        if "insufficient_quota" in str(e):
            from app.services.AIService import _alert_openai_out_of_credit
            await _alert_openai_out_of_credit(str(e))
        return None


def _business_line(business_name: str, category: str, description: str,
                    brand_context: Optional[dict] = None) -> str:
    """The one shared line nearly every content-generation prompt in this
    module opens with — the single choke point for folding in brand voice,
    same reasoning as layer2_generation.brand_palette_clause for images:
    organic content's own write_ad_copy is explicitly "voice-matched to the
    brand playbook when a profile exists" (creative.py's own docstring);
    this module's copy never was, regardless of format, because nothing
    here read brand_context at all. General and additive — reads whatever
    fields brand_context happens to have, works for any brand, and is a
    no-op (identical to the old behaviour) when brand_context is empty."""
    line = f"'{business_name or 'a business'}' (a {category or 'local business'}){(' — ' + description) if description else ''}"
    bc = brand_context or {}
    voice = (bc.get("brand_voice") or "").strip()
    audience = (bc.get("target_audience") or "").strip()
    voice_bits = [v for v in (voice, f"speaking to {audience}" if audience else "") if v]
    if voice_bits:
        line += f" [brand voice/tone: {'; '.join(voice_bits)}]"
    return line


# ── SEED-075: Us vs Them ──────────────────────────────────────────────────

async def _content_us_vs_them(business_name: str, category: str, description: str,
                              brand_context: Optional[dict] = None) -> Optional[list]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, "
        "write 2-3 short comparison rows contrasting the OLD/informal way people currently "
        "handle this against how this business does it.\n"
        "HARD RULE: the 'them' side must name a generic METHOD ('buying at the market', "
        "'doing it yourself', 'guesswork', 'waiting days'), never a specific competitor or "
        "brand name — this is enforced downstream and a named business will be rejected.\n"
        "Keep every 'them' and 'us' value to at most 6 words — short phrases, not sentences "
        "(they are set large and wrap badly past two lines).\n"
        "Return JSON: {\"rows\": [{\"label\": \"short row label e.g. Price\", "
        "\"them\": \"...\", \"us\": \"...\"}, ...]}. Exactly 2 rows unless a 3rd is clearly "
        "worth it. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not isinstance(d.get("rows"), list):
        return None
    rows = [
        (str(r.get("label", "")).strip(), str(r.get("them", "")).strip(), str(r.get("us", "")).strip())
        for r in d["rows"] if isinstance(r, dict)
    ]
    rows = [r for r in rows if all(r)]
    return rows[:3] or None


async def _generate_elegant_background(scene_description: str, brand_context: Optional[dict]) -> Optional[str]:
    """A purely decorative AI backdrop for this module's 4 "drawn"
    formats (Receipt, Us vs Them, Borrowed Interface, Text Only) — asset_
    source='drawn' formats that otherwise render on one flat colour with
    no photography at all, which read as unfinished next to organic
    content's own AI-generated posters. Every one of these formats
    already draws its real content (a card, a bubble, a text block) on
    its own solid, opaque fill — never directly on this backdrop — so a
    failed/missing background never touches legibility or correctness:
    this returns None on any failure rather than raising, and each
    caller's build_document falls back to its original flat-colour
    canvas exactly as it did before this existed."""
    try:
        return await generate_scene(scene_description, size=f"{_CANVAS_SIZE[0]}x{_CANVAS_SIZE[1]}", brand_context=brand_context)
    except SceneGenerationFailed as e:
        print(f"[VSG01] Decorative background generation failed, falling back to flat colour: {e}", flush=True)
        return None


async def _build_us_vs_them(business_name: str, category: str, description: str, tokens: dict,
                            photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                            brand_context: Optional[dict] = None):
    rows = await _content_us_vs_them(business_name, category, description, brand_context)
    if not rows:
        return None
    background_url = await _generate_elegant_background(
        "A purely abstract, non-representational background — soft blurred "
        "colour gradients and gentle ambient light only, premium and calm. "
        "Absolutely no people, faces, hands, body parts, or any recognisable "
        "object, product, or scene of any kind — pure colour, light, and soft "
        "shape only, non-distracting, suitable for a comparison graphic to sit "
        "on top of.",
        brand_context,
    )
    try:
        document = us_vs_them.build_document(
            rows, canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
            background_url=background_url,
        )
    except Exception as e:
        print(f"[VSG01] Us vs Them build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Us vs Them failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-087: Borrowed Interface ──────────────────────────────────────────

async def _content_borrowed_interface(business_name: str, category: str, description: str,
                                      correction: str = "",
                                      brand_context: Optional[dict] = None) -> Optional[list]:
    """Real per-message character cap, not just a style ask — live-confirmed
    a 4-turn exchange with realistically longer messages (2-4 wrapped lines
    each) rendered with its final bubble clipped off the canvas (see
    borrowed_interface.ExchangeOverflowsCanvas, added after that failure).
    A chat message is naturally short anyway, so this constraint should
    read as normal phrasing, not a compression exercise."""
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, write a "
        "short, realistic WhatsApp-style exchange (3-4 messages total) between a customer and the "
        "business, ending with the business's offer or answer as the final message. Plausible "
        "casual Nigerian phrasing, no emoji spam.\n"
        "HARD RULE (§2.6: 'must not misrepresent price, delivery or availability'): the final "
        "message must NEVER state a specific price, delivery fee, delivery area, or availability "
        "claim UNLESS that exact detail appears verbatim in the business's own text above — never "
        "invent one. If no real detail is stated, make the final message a natural next step "
        "instead ('Sure, what do you need?', 'Let me get your details'), never a specific "
        "commitment that isn't backed by the business's own words.\n"
        "HARD LIMIT: each message must be 45 characters or fewer, including spaces and "
        "punctuation — a real chat message, not a paragraph. Correctly-sized examples: 'Do "
        "you deliver to Lekki?' (22 chars), 'Sure, what do you need?' (24 chars).\n"
        "Return JSON: {\"turns\": [{\"speaker\": \"them\"|\"us\", \"message\": \"...\", "
        "\"timestamp\": \"e.g. 10:41 AM\"}, ...]}. 3-4 turns, last turn speaker must be \"us\". "
        f"{('CORRECTION: ' + correction) if correction else ''}\n"
        "Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not isinstance(d.get("turns"), list):
        return None
    turns = [
        (str(t.get("speaker", "")).strip(), str(t.get("message", "")).strip(), str(t.get("timestamp", "")).strip())
        for t in d["turns"] if isinstance(t, dict)
    ]
    turns = [t for t in turns if t[0] in ("them", "us") and t[1] and t[2]][:4]
    if not turns:
        return None
    # Enforced backstop, not just a prompt instruction — same belt-and-
    # suspenders pattern as Problem/Solution's own digit guard. A ₦ amount,
    # percentage, or 3+ digit number in ANY turn that doesn't appear
    # verbatim in the business's own description is a fabricated price/
    # fee/availability claim, exactly what §2.6 forbids this format from
    # ever showing — reject the whole exchange rather than surgically
    # editing a chat bubble into broken grammar.
    src_digits = re.sub(r"[^\d]", "", description or "")
    for _, message, _ in turns:
        for tok in re.findall(r"₦\s?[\d,]+|\d[\d,]*\s?%|\d[\d,]{2,}", message):
            if re.sub(r"[^\d]", "", tok) not in src_digits:
                return None
    return turns


async def _build_borrowed_interface(business_name: str, category: str, description: str, tokens: dict,
                                    photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                    brand_context: Optional[dict] = None):
    # brand_logo_url accepted (uniform call signature across every builder) but
    # deliberately never used — VSG-01-PROMPTS v2 §6.6: "A logo destroys this
    # format" (brand_mark="prohibited"). render_vsg01_creative never actually
    # passes one here (gated on format_def.brand_mark), so this is belt-and-
    # suspenders, not the real enforcement point.
    turns = await _content_borrowed_interface(business_name, category, description, brand_context=brand_context)
    if not turns:
        return None
    background_url = await _generate_elegant_background(
        "A softly blurred, realistic phone-screen ambient background — subtle "
        "warm bokeh light, calm and minimal, like the soft background blur "
        "behind a genuine phone screenshot, not a busy or distracting scene. "
        "Pure light and blur only — no people, faces, hands, body parts, or "
        "any recognisable object or product anywhere in frame.",
        brand_context,
    )

    def _try_build(t):
        return borrowed_interface.build_document(
            t, canvas_size=_CANVAS_SIZE, tokens=tokens, background_url=background_url,
        )

    try:
        document = _try_build(turns)
    except borrowed_interface.ExchangeOverflowsCanvas as e:
        # Same "regenerate once with the exact correction, then accept
        # whatever comes back" pattern as Text on a Face/News Headline's
        # own overflow retries — the 45-char cap above should make this
        # rare, not eliminate it outright.
        print(f"[VSG01] Borrowed Interface exchange overflowed canvas, retrying shorter: {e}", flush=True)
        retry_turns = await _content_borrowed_interface(
            business_name, category, description,
            correction=f"your last attempt overflowed the canvas ({e}). Make every message "
                       "noticeably shorter this time — 30 characters or fewer.",
            brand_context=brand_context,
        )
        if not retry_turns:
            return None
        try:
            document = _try_build(retry_turns)
        except Exception as e2:
            print(f"[VSG01] Borrowed Interface still failed after retry: {e2}", flush=True)
            return None
    except Exception as e:
        print(f"[VSG01] Borrowed Interface build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Borrowed Interface failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-080: Problem / Solution ──────────────────────────────────────────

# The content-generation LLM's menu of settings to pick from — aliased to
# visual_slots.NIGERIAN_SETTINGS (the single source of truth that
# resolve_nigerian_setting() actually validates against) rather than a
# hand-maintained copy. A second copy is exactly how this drifted before:
# this tuple offered 8 while visual_slots.py's real enforced vocabulary
# also had only those same 8, so it happened to match by coincidence, not
# by construction — expanding one without the other (as v3's 15-entry
# vocabulary would have, had this stayed a copy) would have let the LLM
# pick a setting resolve_nigerian_setting() then rejects with
# InvalidSlotValue.
_NIGERIAN_SETTINGS = NIGERIAN_SETTINGS


# §8: "Seasonal context should remain a slot, not a new format" — resolved
# here from today's date and threaded into the real generation call sites
# below, rather than left as an unreachable parameter nothing ever
# populates (the same "built but never wired" gap this session already
# found and fixed once for the corpus seed — see the Phase 2 plan).
#
# Deliberately covers only THREE of §8's nine possible values — the ones
# with a fixed, non-controversial calendar window a Nigerian business
# audience would recognise as "in season" without needing external data:
# salary week (a short, high-salience commercial window), Detty December
# (all of December), and back-to-school season (September, when most
# Nigerian school terms resume). Harmattan/rainy/dry season are real but
# diffuse weather windows that would fire for months at a time with no
# single clear boundary, and Easter/Eid are movable feasts that need a
# real calendar computation (lunar for Eid) to place correctly — guessing
# either wrong is worse than the honest None returned here for the rest of
# the year. §8's own text makes this restraint the correct default anyway:
# "Do not add seasonal decorations merely because the slot is populated."
def _resolve_current_seasonal_context(today: Optional[date] = None) -> Optional[str]:
    d = today or date.today()
    if d.month == 12:
        return "Detty December"
    if d.month == 9:
        return "back-to-school season"
    if d.day >= 28 or d.day <= 2:
        return "salary week"
    return None


async def _content_problem_solution(business_name: str, category: str, description: str,
                                    brand_context: Optional[dict] = None) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, describe "
        "the PROBLEM this business solves and the SOLUTION it offers, for a two-zone visual ad.\n"
        "- problem_situation: a short, concrete VISUAL scene of the problem (what a camera would "
        "see, no people's names, no brand names, no location names)\n"
        "- solution_situation: a short, concrete VISUAL scene of the resolved state\n"
        "- problem_text: the problem stated as a felt pain or consequence, <=8 words. Do NOT "
        "state a naira figure, percentage, or any number UNLESS that exact figure appears "
        "verbatim in the business's own text above — never estimate or invent one.\n"
        "- solution_text: the concrete RESULT the customer gets, <=8 words — a specific "
        "changed situation, not a slogan. GOOD: 'Your posts ready a week ahead', 'Customers "
        "message you first'. BAD (vague sentiment, reject these): 'Elevate your brand', "
        "'Compelling storytelling', 'Unlock your potential', 'Take it to the next level'. "
        "Same number rule: no invented figures.\n"
        f"- nigerian_setting: pick the single best-fitting option, copied EXACTLY, from this list: "
        f"{list(_NIGERIAN_SETTINGS)}\n"
        "Return ONLY the JSON with exactly these 5 keys."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    setting = str(d.get("nigerian_setting", "")).strip()
    if setting not in _NIGERIAN_SETTINGS:
        setting = _NIGERIAN_SETTINGS[0]
    out = {
        "problem_situation": str(d.get("problem_situation", "")).strip(),
        "solution_situation": str(d.get("solution_situation", "")).strip(),
        "problem_text": str(d.get("problem_text", "")).strip(),
        "solution_text": str(d.get("solution_text", "")).strip(),
        "nigerian_setting": setting,
    }
    if not all([out["problem_situation"], out["solution_situation"], out["problem_text"], out["solution_text"]]):
        return None
    # Guard against an invented figure the prompt was told not to produce — a
    # ₦ amount, a percentage, or a 3+ digit number in the headline that isn't
    # in the business's own text. Blank that headline word ("Wasting ₦50,000
    # on chaos" -> "Wasting on chaos") and tidy the spacing; only bail out if
    # blanking leaves nothing.
    src_digits = re.sub(r"[^\d]", "", description or "")
    for key in ("problem_text", "solution_text"):
        for tok in re.findall(r"₦\s?[\d,]+|\d[\d,]*\s?%|\d[\d,]{2,}", out[key]):
            if re.sub(r"[^\d]", "", tok) not in src_digits:
                out[key] = re.sub(r"\s{2,}", " ", out[key].replace(tok, "")).strip(" ,.-")
    if not out["problem_text"] or not out["solution_text"]:
        return None
    return out


async def _build_problem_solution(business_name: str, category: str, description: str, tokens: dict,
                                  photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                  brand_context: Optional[dict] = None):
    content = await _content_problem_solution(business_name, category, description, brand_context)
    if not content:
        return None
    width, height = _CANVAS_SIZE
    zone_size = f"{width}x{height // 2}"
    seasonal_context = _resolve_current_seasonal_context()
    prompts = {
        "problem": problem_solution._problem_prompt(
            content["problem_situation"], content["nigerian_setting"], seasonal_context,
        ),
        "solution": problem_solution._solution_prompt(
            content["solution_situation"], content["problem_situation"], content["nigerian_setting"],
            seasonal_context,
        ),
    }

    async def _gen_zone_passing_skin_check(prompt: str, zone: str) -> Optional[str]:
        """§1.7 — generate the zone, verify skin rendering, and regenerate
        ONCE if a person is rendered outside the deep-brown target range
        (image models lighten skin intermittently). Live-confirmed gap in
        the original version of this retry: it re-sent the IDENTICAL prompt
        on attempt 2 — a pure reroll, no correction — which is why a real
        production run saw the same "medium brown" verdict twice in a row.
        The retry now prepends a short, specific correction naming the
        actual wrong tone the vision check just observed. Prepended (not
        appended) so it survives generate_scene's own length-truncation
        guard regardless of budget pressure — that guard only ever trims
        from the end of the string."""
        current_prompt = prompt
        for attempt in (1, 2):
            url = await generate_scene(current_prompt, size=zone_size, brand_context=brand_context)
            result = await verify_skin_rendering(url)
            if not result["contains_person"] or result["matches_target_range"]:
                return url
            observed = result.get("skin_tone_observed") or "too light"
            print(f"[VSG01] Problem/Solution {zone} zone failed skin-tone check "
                  f"(attempt {attempt}/2): {result['notes']}", flush=True)
            current_prompt = (
                f"CRITICAL: skin must be deep brown to dark brown, NOT {observed} "
                f"as last time. {prompt}"
            )
        return None

    # The two zones are fully independent generations — no reason to make a
    # user wait for them sequentially. Live-confirmed real-world impact:
    # a single skin-tone retry on the solution zone alone (after the
    # problem zone had already run) pushed one real request past 165
    # seconds end to end for this format alone, well past the ALB's 120s
    # idle timeout — the frontend reports that as a bare "Network Error"
    # with no indication generation was actually still succeeding server-
    # side. Running both concurrently is a pure latency win with no
    # behaviour change: same two prompts, same retry logic, same failure
    # handling — just not waited on one after the other.
    try:
        problem_url, solution_url = await asyncio.gather(
            _gen_zone_passing_skin_check(prompts["problem"], "problem"),
            _gen_zone_passing_skin_check(prompts["solution"], "solution"),
        )
    except SceneGenerationFailed as e:
        print(f"[VSG01] Problem/Solution scene generation failed: {e}", flush=True)
        return None
    if problem_url is None or solution_url is None:
        return None

    try:
        # build_document already calls legibility.assert_legible() itself
        # (the one format module in the library that self-checks — see its
        # own docstring) — no separate check_legibility call needed here.
        document = problem_solution.build_document(
            problem_url, solution_url, content["problem_text"], content["solution_text"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Problem/Solution build failed: {e}", flush=True)
        return None
    return document


# ── Shared: extract a REAL quote, never compose one ───────────────────────

def _normalise_for_match(text: str) -> str:
    import re
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", re.sub(r"[\"'.,!?;:()\-–—]", " ", text.lower())).strip()


def _appears_in(fragment: str, source: str) -> bool:
    """Whether `fragment` is in `source` word for word, ignoring case, quote marks
    and punctuation. An empty fragment never counts."""
    f = _normalise_for_match(fragment)
    return bool(f) and f in _normalise_for_match(source)


async def _extract_real_quote(description: str) -> Optional[dict]:
    """Used by Review Card and Testimonial+Offer — both require a genuine
    customer quote (§2.1/§2.2). Only ever extracts what the caller's own
    text already states verbatim; returns None (not a fabricated quote) the
    moment nothing real is there. `star_rating` is likewise only ever a real
    rating the text states, never inferred from sentiment."""
    if not (description or "").strip():
        return None
    prompt = (
        f"Below is a business's own description/context text:\n\n{description}\n\n"
        "Does this text contain an ACTUAL customer quote/review, stated by the business "
        "itself (not something you should write)? If yes, extract it VERBATIM along with "
        "who said it and, only if a real star rating (1-5) is explicitly stated, that rating. "
        "If there is no real quote in the text, return quote as null — do NOT invent, "
        "paraphrase, or infer one from general sentiment.\n"
        "Return JSON: {\"quote\": \"...\" or null, \"attribution\": \"e.g. Ngozi A.\" or \"\", "
        "\"star_rating\": 1-5 or null}. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not d.get("quote"):
        return None
    # The model is asked for a verbatim quote but nothing made it one: a paraphrase
    # or an invented line presented as a customer's words is a fabricated
    # testimonial (Ads PRD CR-07). Both the words and who said them must appear in
    # the business's own text, or the format is not used.
    if not _appears_in(str(d["quote"]), description) or \
            not _appears_in(str(d.get("attribution") or ""), description):
        return None
    star = d.get("star_rating")
    star = int(star) if isinstance(star, (int, float)) and 1 <= int(star) <= 5 else None
    return {
        "quote": str(d["quote"]).strip(),
        "attribution": str(d.get("attribution", "")).strip(),
        "star_rating": star,
    }


# ── SEED-081: Receipt ──────────────────────────────────────────────────────

async def _content_receipt(description: str) -> Optional[dict]:
    """Only ever fires when the business's own words already state real,
    currently-honoured item + price pairs (§2.5) — never invents pricing.
    Empty result is the expected common case, not a failure."""
    if not (description or "").strip():
        return None
    prompt = (
        f"Below is a business's own description/context text:\n\n{description}\n\n"
        "Does this text state REAL, specific item names with REAL prices (e.g. "
        "'haircut ₦3000, beard trim ₦1500')? If yes, extract each pair VERBATIM as "
        "given — never estimate, round, or invent a price that isn't explicitly stated. "
        "If no real item+price pairs are stated, return items as an empty list.\n"
        "Return JSON: {\"items\": [{\"name\": \"...\", \"price\": \"e.g. ₦3,000\"}, ...], "
        "\"total_label\": \"e.g. Total\", \"total_amount\": \"the sum, formatted like the "
        "item prices, or empty string if items don't cleanly sum\"}. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not isinstance(d.get("items"), list) or not d["items"]:
        return None
    items = [
        (str(i.get("name", "")).strip(), str(i.get("price", "")).strip())
        for i in d["items"] if isinstance(i, dict)
    ]
    items = [i for i in items if all(i)]
    if not items:
        return None
    # receipt.py's own layout has no upper bound on item count (its card
    # grows to fit whatever it's given) and legibility.py's check never
    # validates content height against the canvas at all — confirmed
    # reading that module directly. A truncated *display* of, say, the
    # first 6 of 8 real items would still be individually verbatim, but
    # total_amount (the business's own stated full total) would then no
    # longer match what's actually shown — a real, different accuracy
    # problem, arguably worse than the format simply not firing. Same
    # "under-fire rather than over-fire" direction this module's own
    # docstring already commits to for quotes/prices: too many items to
    # show accurately means this format doesn't fit this business's offer,
    # not something to silently truncate.
    if len(items) > 6:
        return None
    return {
        "items": items,
        "total_label": str(d.get("total_label", "Total")).strip() or "Total",
        "total_amount": str(d.get("total_amount", "")).strip(),
    }


async def _build_receipt(business_name: str, category: str, description: str, tokens: dict,
                         photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                         brand_context: Optional[dict] = None):
    # brand_context doesn't reach the CONTENT here: Receipt's own contract is
    # verbatim-fact extraction only ("every figure real and honoured") —
    # there's no styled prose for a voice clause to influence. It does reach
    # the decorative background below, same as every other "drawn" format.
    from .ad_formats import receipt
    content = await _content_receipt(description)
    if not content or not content["total_amount"]:
        return None
    background_url = await _generate_elegant_background(
        "A softly blurred, elegant flat surface for a premium receipt display "
        "— warm wood grain, subtle marble, or soft linen texture, gentle "
        "natural window light, shallow depth of field, calm and uncluttered, "
        "like a stylish invoice photographed on a boutique shop counter. Only "
        "the surface and ambient light — no people, faces, hands, or any "
        "other object or product anywhere in frame.",
        brand_context,
    )
    try:
        document = receipt.build_document(
            content["items"], content["total_label"], content["total_amount"],
            business_name=business_name, canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url, background_url=background_url,
        )
    except Exception as e:
        print(f"[VSG01] Receipt build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Receipt failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-093: Review Card (needs a real, attested product photo) ─────────

async def _build_review_card(business_name: str, category: str, description: str, tokens: dict,
                             photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                             brand_context: Optional[dict] = None):
    # brand_context accepted (uniform builder signature) but unused: the
    # quote/rating here are a real customer's own words, extracted verbatim
    # by _extract_real_quote — nothing here is written prose a voice clause
    # could influence.
    if not photo_url:
        return None
    real = await _extract_real_quote(description)
    if not real:
        return None
    try:
        document = review_card.build_document(
            photo_url, real["quote"], real["attribution"],
            star_rating=real["star_rating"], canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Review Card build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Review Card failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-082: Text on a Face (needs an attested photo of the owner or their team) ─────

async def _content_text_on_a_face(business_name: str, category: str, description: str,
                                  correction: str = "",
                                  brand_context: Optional[dict] = None) -> Optional[str]:
    """The seller's own position/observed situation — safe to compose (this
    is not a claimed quote from anyone), but must clear the format's own
    ViewerPresumption/DisallowedPersonalTopic guards, which build_document
    enforces regardless of what this returns.

    §2.7's "one short line" is measured against real pixel width at a large
    bold font (~968px plate, 48px bold) — live-confirmed a statement written
    to only the vague instruction "one short line" still wrapped to three
    lines. ~20 characters is the real, tested-safe budget (a 35-character
    line already measured ~1252px, well past the plate), not a stylistic
    preference — the prompt states it as a hard number for exactly that
    reason."""
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, write ONE "
        "short first-person line (the business owner's own position or an observed situation about "
        "their work) to sit across a photo of them.\n"
        "HARD LIMIT: 20 characters or fewer, INCLUDING spaces and punctuation — this is a real pixel-"
        "width constraint, not a style preference. 3-4 words maximum. Correctly-sized examples: "
        "'No job too hard.' (16 chars), 'I never give up.' (17 chars), 'Real fixes, fast.' (17 chars).\n"
        "NOT a question, NOT a presumption about the reader ('are you struggling with...'), and never "
        "touching health, body, finances, or personal circumstance.\n"
        f"{('CORRECTION: ' + correction) if correction else ''}\n"
        "Return JSON: {\"statement\": \"...\"}. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    statement = str(d.get("statement", "")).strip()
    return statement or None


async def _build_text_on_a_face(business_name: str, category: str, description: str, tokens: dict,
                                photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                brand_context: Optional[dict] = None):
    if not photo_url:
        return None
    statement = await _content_text_on_a_face(business_name, category, description, brand_context=brand_context)
    if not statement:
        return None

    def _try_build(stmt: str):
        # permission_on_file=True: the attestation step upstream (the user
        # confirming this is a real customer's photo before this call ever
        # happens) IS the permission confirmation this format requires.
        return text_on_a_face.build_document(
            photo_url, stmt, permission_on_file=True, canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url, speaker="owner",
        )

    try:
        return _try_build(statement)
    except text_on_a_face.TextNotOneLine as e:
        # The one correctable failure here — same "regenerate once with the
        # exact correction, then accept whatever comes back" pattern as
        # creative.py's write_ad_copy leakage retry, not an open-ended loop.
        print(f"[VSG01] Text on a Face statement too long ({len(statement)} chars), retrying shorter: {e}",
              flush=True)
        retry_statement = await _content_text_on_a_face(
            business_name, category, description,
            correction=f"your last attempt ({statement!r}, {len(statement)} chars) was too long. "
                       "Make it shorter — 15 characters or fewer this time.",
            brand_context=brand_context,
        )
        if not retry_statement:
            return None
        try:
            return _try_build(retry_statement)
        except Exception as e2:
            print(f"[VSG01] Text on a Face still failed after retry: {e2}", flush=True)
            return None
    except Exception as e:
        # PermissionNotOnFile/ViewerPresumption/DisallowedPersonalTopic — a guard
        # tripping on CONTENT (not length) means "skip this format this time,"
        # not something a blind retry with the same casual prompt reliably fixes.
        print(f"[VSG01] Text on a Face build failed: {e}", flush=True)
        return None


# ── SEED-074: Testimonial + Offer, person path (needs a real customer photo) ─

async def _content_offer(business_name: str, category: str, description: str,
                         brand_context: Optional[dict] = None) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, write a "
        "short, concrete offer line and, only if the text above states one, real price/terms.\n"
        "Return JSON: {\"offer_text\": \"...\", \"price_or_terms\": \"...\" or \"\"}. "
        "Never invent a price — leave price_or_terms empty if none was stated. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    offer_text = str(d.get("offer_text", "")).strip()
    if not offer_text:
        return None
    return {"offer_text": offer_text, "price_or_terms": str(d.get("price_or_terms", "")).strip() or None}


async def _build_testimonial_offer(business_name: str, category: str, description: str, tokens: dict,
                                   photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                   brand_context: Optional[dict] = None):
    if not photo_url:
        return None
    real = await _extract_real_quote(description)
    offer = await _content_offer(business_name, category, description, brand_context)
    if not real or not offer:
        return None
    try:
        document = testimonial_offer.build_document(
            photo_url, real["quote"], real["attribution"],
            offer["offer_text"], permission_on_file=True,
            price_or_terms=offer["price_or_terms"], canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Testimonial + Offer build failed: {e}", flush=True)
        return None
    return document


# ── SEED-088: Starter Pack (recomposite-only — needs a clean product cutout) ─

async def _content_starter_pack(business_name: str, category: str, description: str,
                                brand_context: Optional[dict] = None) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, this "
        "business's product will sit in a flat-lay grid among 3-6 OTHER everyday items that "
        "belong to the same lifestyle/identity as the business's real customer (VSG-01 Starter "
        "Pack format — never build this on an ethnic, regional, or religious stereotype; the "
        "identity must be one the audience would claim willingly, e.g. 'a Lagos gym-goer's kit', "
        "not a group label).\n"
        "- title: the identity this pack is for, as the meme says it, e.g. 'The Lagos gym-goer starter pack' "
        "(<=7 words, ends with 'starter pack')\n"
        "- product_label: a short label for the business's own product (from its name/category)\n"
        "- items: 3-6 objects, each {\"description\": \"a short scene description for generating a "
        "flat-lay photo of it, e.g. 'a pair of running shoes'\", \"label\": \"short caption\"}\n"
        f"Every label (product_label included) sits under its photo on ONE line: 1-2 whole words, at most "
        f"{_STARTER_LABEL_CHARS} characters, e.g. 'Gym shoes', 'Zobo drink', 'Health app'. Never abbreviate "
        "or cut a word short ('Appt Cal' is wrong; 'Calendar' is right).\n"
        "Return ONLY the JSON with exactly these 3 keys."
    )
    d = await _call_content_model(prompt)
    if not d or not isinstance(d.get("items"), list):
        return None
    items = [
        (str(i.get("description", "")).strip(), str(i.get("label", "")).strip())
        for i in d["items"] if isinstance(i, dict)
    ]
    items = [i for i in items if all(i)][:6]
    if len(items) < 3:
        return None
    product_label = str(d.get("product_label", "")).strip() or business_name or category or "Our product"
    fitted = await _fit_starter_labels(business_name, category, product_label, items,
                                       _brand_font(brand_context))
    if fitted:
        fitted["title"] = str(d.get("title", "")).strip() or None
    return fitted


def _brand_font(brand_context: Optional[dict]) -> str:
    from .ad_formats._type import headline_family
    return headline_family(brand_context, "Inter")


# Grid cells at the most items (7 → 3 columns) leave ~300px: about this many characters at the 42px floor.
_STARTER_LABEL_CHARS = 12


async def _fit_starter_labels(business_name: str, category: str, product_label: str,
                              items: list, font_family: Optional[str] = None) -> Optional[dict]:
    """Every label must sit on one line under its photo (starter_pack's grid has room for one).
    A label that doesn't fit gets one shorter rewrite; an extra item whose label still won't fit is
    left out (3 must remain), and the product's own label falls back to the business name."""
    from .ad_formats import starter_pack

    cell_w = starter_pack.cell_width_for(len(items) + 1)
    labels = [product_label] + [label for _, label in items]
    too_long = [l for l in labels if not starter_pack.label_fits(l, cell_w, font_family)]
    if too_long:
        d = await _call_content_model(
            "Shorten each of these image captions to 1-2 whole words, at most "
            f"{_STARTER_LABEL_CHARS} characters, keeping the meaning. Never abbreviate or cut a word "
            "short; pick a shorter word instead: "
            + json.dumps(too_long)
            + '. Return ONLY JSON: {"labels": ["...", ...]} in the same order.'
        )
        short = (d or {}).get("labels") if isinstance(d, dict) else None
        if isinstance(short, list) and len(short) == len(too_long):
            swap = {old: str(new).strip() for old, new in zip(too_long, short) if str(new).strip()}
            product_label = swap.get(product_label, product_label)
            items = [(desc, swap.get(label, label)) for desc, label in items]
    items = [(desc, label) for desc, label in items if starter_pack.label_fits(label, cell_w, font_family)]
    if len(items) < 3:
        return None
    cell_w = starter_pack.cell_width_for(len(items) + 1)
    if not starter_pack.label_fits(product_label, cell_w, font_family):
        product_label = next(
            (c for c in (business_name, category) if c and starter_pack.label_fits(c, cell_w, font_family)), None
        )
        if not product_label:
            return None
    return {"product_label": product_label, "items": items}


async def _build_starter_pack(business_name: str, category: str, description: str, tokens: dict,
                              photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                              brand_context: Optional[dict] = None):
    if not photo_url:
        return None
    content = await _content_starter_pack(business_name, category, description, brand_context)
    if not content:
        return None
    item_descriptions = [i[0] for i in content["items"]]
    item_labels = [i[1] for i in content["items"]]
    width, height = _CANVAS_SIZE
    import math
    cols = math.ceil(math.sqrt(len(item_descriptions) + 1))
    cell_size = f"{width // cols}x{width // cols}"
    seasonal_context = _resolve_current_seasonal_context()
    try:
        # 3-6 fully independent item generations — sequential here meant a
        # worst case of 6 back-to-back gpt-image-2 calls (each observed
        # taking anywhere from ~10s to ~60s+ in production) stacking into
        # several minutes for one request, well past both the frontend's
        # own 240s timeout and the ALB's 120s idle timeout (see
        # _build_problem_solution's identical fix for the real production
        # timeline that surfaced this). Same prompts, same failure
        # handling — just generated concurrently instead of one at a time.
        item_urls = await asyncio.gather(*(
            generate_scene(
                starter_pack._item_prompt(desc, seasonal_context=seasonal_context), size=cell_size,
                brand_context=brand_context,
            )
            for desc in item_descriptions
        ))
    except SceneGenerationFailed as e:
        print(f"[VSG01] Starter Pack item generation failed: {e}", flush=True)
        return None

    # The owner's real product, cut out so it sits flush among the flat-lay items (the format's spec);
    # the photo as it is only when the cut-out can't be made.
    product_url = await _cutout(photo_url) or photo_url
    font = _brand_font(brand_context)
    try:
        document = starter_pack.build_document(
            item_urls, item_labels, product_url, content["product_label"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
            title=content.get("title"), font_family=font,
        )
    except Exception as e:
        print(f"[VSG01] Starter Pack build failed: {e}", flush=True)
        return None
    return document


# ── SEED-096: Price-Led Offer (needs a real, attested product photo) ─

async def _content_price_led_offer(description: str) -> Optional[dict]:
    """Only ever fires when the business's own words already state a real,
    currently-honoured price (VSG-01-PROMPTS v2 §6.13) — never invents one,
    same contract as _content_receipt."""
    if not (description or "").strip():
        return None
    prompt = (
        f"Below is a business's own description/context text:\n\n{description}\n\n"
        "Does this text state a REAL, specific price for what's being sold (e.g. "
        "'sourdough bread ₦3,500')? If yes, extract it VERBATIM — never estimate, "
        "round, or invent a price that isn't explicitly stated. Also extract, ONLY if "
        "explicitly stated: a delivery area, a payment method, a short call-to-action "
        "line, and a genuine PRIOR price (only if the text says it was actually charged "
        "before, e.g. 'was ₦5,000 now ₦3,500').\n"
        "If no real price is stated, return price as an empty string.\n"
        "Return JSON: {\"price\": \"e.g. ₦3,500\", \"was_price\": \"...\" or \"\", "
        "\"delivery_line\": \"...\" or \"\", \"payment_line\": \"...\" or \"\", "
        "\"action_line\": \"...\" or \"\"}. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not str(d.get("price", "")).strip():
        return None
    return {
        "price": str(d.get("price", "")).strip(),
        "was_price": str(d.get("was_price", "")).strip() or None,
        "delivery_line": str(d.get("delivery_line", "")).strip() or None,
        "payment_line": str(d.get("payment_line", "")).strip() or None,
        "action_line": str(d.get("action_line", "")).strip() or None,
    }


async def _build_price_led_offer(business_name: str, category: str, description: str, tokens: dict,
                                 photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                 brand_context: Optional[dict] = None):
    # brand_context accepted (uniform builder signature) but unused: same
    # verbatim-fact contract as Receipt — a real, currently-honoured price
    # extracted as-is, never styled prose.
    if not photo_url:
        return None
    content = await _content_price_led_offer(description)
    if not content:
        return None
    from .ad_formats import price_led_offer
    try:
        document = price_led_offer.build_document(
            photo_url, content["price"],
            delivery_line=content["delivery_line"], payment_line=content["payment_line"],
            action_line=content["action_line"], was_price=content["was_price"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Price-Led Offer build failed: {e}", flush=True)
        return None
    return document


# ── SEED-097: Text-Only (no photo at all) ────────────────────────

async def _content_text_only(business_name: str, category: str, description: str,
                             brand_context: Optional[dict] = None) -> Optional[dict]:
    """Same truthfulness contract as _content_receipt/_content_price_led_offer
    — this format has NO image to fall back on, so §6.14 is explicit the
    headline must carry a real fact, never invented sentiment. Only ever
    fires when the business's own words state something concrete."""
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, does the "
        "text above state one REAL, concrete fact worth leading with — a price, a delivery area, "
        "a specific offer, an opening date? This format has NO image at all, so the headline must "
        "carry real information, never vague sentiment like 'we're the best'.\n"
        "If nothing concrete is stated, return headline as an empty string — do not invent one.\n"
        "Return JSON: {\"headline\": \"the one real fact, as a short punchy line\", "
        "\"subline\": \"...\" or \"\", \"action_line\": \"...\" or \"\"}. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not str(d.get("headline", "")).strip():
        return None
    return {
        "headline": str(d.get("headline", "")).strip(),
        "subline": str(d.get("subline", "")).strip() or None,
        "action_line": str(d.get("action_line", "")).strip() or None,
    }


async def _build_text_only(business_name: str, category: str, description: str, tokens: dict,
                           photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                           brand_context: Optional[dict] = None):
    content = await _content_text_only(business_name, category, description, brand_context)
    if not content:
        return None
    from .ad_formats import text_only
    background_url = await _generate_elegant_background(
        "A purely abstract, non-representational background — soft blurred "
        "colour gradients and gentle ambient light only, premium and calm, "
        "like a high-end brand's own social media backdrop. Absolutely no "
        "people, faces, hands, body parts, or any recognisable object, "
        "product, or scene of any kind — pure colour, light, and soft shape "
        "only.",
        brand_context,
    )
    try:
        document = text_only.build_document(
            content["headline"], subline=content["subline"], action_line=content["action_line"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
            background_url=background_url,
        )
    except Exception as e:
        print(f"[VSG01] Text-Only build failed: {e}", flush=True)
        return None
    return document


# ── SEED-098: Work In Progress (generated scene — see module
# docstring on why this doesn't yet accept a real work photo) ─────────────

def _is_true(value) -> bool:
    """A model's boolean, which may come back as true or as the string "true"."""
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


async def _content_work_in_progress(business_name: str, category: str, description: str,
                                    brand_context: Optional[dict] = None) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, describe "
        "the everyday hands-on WORK this business does (e.g. 'installing solar panels', 'fixing a "
        "burst pipe', 'repairing a generator') and write one short line naming what's being done "
        "and, if known, where they serve (e.g. 'Solar install underway — Lekki Phase 1').\n"
        "- trade_activity: a short, concrete VISUAL scene for a documentary photo (what a camera "
        "would see — hands, tools, the job partially complete), no brand/person names\n"
        "- statement: the short line, 6 words or fewer\n"
        f"- nigerian_setting: pick the single best-fitting option, copied EXACTLY, from this list: "
        f"{list(_NIGERIAN_SETTINGS)}\n"
        "- applies: true ONLY if THIS business itself does visible, hands-on physical work a camera "
        "could photograph on site (trades, repairs, installation, construction, food preparation, "
        "tailoring, cleaning, farming...). false for software, apps, online services, consulting, "
        "finance, education, agencies and anything done at a screen or desk.\n"
        "The examples above only show the STYLE. Never reuse them: the activity must be this "
        "business's own real work. If applies is false, leave the other fields empty.\n"
        "Return ONLY the JSON with exactly these 4 keys."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    # This format shows a tradesperson mid-job. For a business with no such work
    # the model used to copy an example from the prompt — a software company got
    # an ad of plumbers fixing a burst pipe (seen on dev). Returning None makes
    # select_and_render_vsg01_creative move on to the next format.
    if not _is_true(d.get("applies")):
        print(f"[VSG01] Work In Progress doesn't fit {business_name or 'this business'} (no hands-on work) — skipping", flush=True)
        return None
    trade_activity = str(d.get("trade_activity", "")).strip()
    statement = str(d.get("statement", "")).strip()
    setting = str(d.get("nigerian_setting", "")).strip()
    if setting not in _NIGERIAN_SETTINGS:
        setting = _NIGERIAN_SETTINGS[0]
    if not trade_activity or not statement:
        return None
    return {"trade_activity": trade_activity, "statement": statement, "nigerian_setting": setting}


async def _build_work_in_progress(business_name: str, category: str, description: str, tokens: dict,
                                  photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                  brand_context: Optional[dict] = None):
    content = await _content_work_in_progress(business_name, category, description, brand_context)
    if not content:
        return None
    from .ad_formats import work_in_progress
    try:
        scene_url = await generate_scene(
            work_in_progress._scene_prompt(
                content["trade_activity"], content["nigerian_setting"], _resolve_current_seasonal_context(),
            ),
            size=f"{_CANVAS_SIZE[0]}x{_CANVAS_SIZE[1]}",
            brand_context=brand_context,
        )
    except SceneGenerationFailed as e:
        print(f"[VSG01] Work In Progress scene generation failed: {e}", flush=True)
        return None

    result = await verify_skin_rendering(scene_url)
    if result["contains_person"] and not result["matches_target_range"]:
        print(f"[VSG01] Work In Progress failed skin-tone check: {result['notes']}", flush=True)
        return None

    try:
        document = work_in_progress.build_document(
            scene_url, content["statement"], canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Work In Progress build failed: {e}", flush=True)
        return None
    return document


# ── SEED-077: News Headline (generate path only here — see module docstring
# on the isolation-cap gate; upload_as_is of a real event photo is also
# permitted by the format itself but has no attestation type to trigger it) ─

_NEWS_FILLER = {
    "the", "and", "for", "now", "our", "your", "you", "with", "new", "here", "today",
    "just", "all", "are", "get", "its", "this", "that", "from", "into", "out", "now",
}


def _news_grounded(text: str, source: str) -> bool:
    """True when the words that carry the news are the client's own. "Real
    announcements only" was an instruction the model could break, and did: a brief
    with no announcement in it produced "New branch in Lekki opens". A news ad is a
    claim with the advertiser's name on it, so it has to be checkable against what
    they actually wrote. A word counts when the brief has a word starting with the
    same four letters ("open" ~ "opening")."""
    words = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
             if len(w) >= 3 and w not in _NEWS_FILLER]
    if not words:
        return False
    stems = {w[:4] for w in re.findall(r"[a-z0-9]+", (source or "").lower()) if len(w) >= 3}
    found = sum(1 for w in words if w[:4] in stems)
    return found * 2 >= len(words) * 1 and found >= max(1, len(words) - 1)


async def _content_news_headline(business_name: str, category: str, description: str,
                                 correction: str = "",
                                 brand_context: Optional[dict] = None) -> Optional[dict]:
    """§2.8: 'Real announcements only.' Same verbatim-fact contract as
    _content_text_only/_content_receipt — only ever fires when the
    business's own words already state a genuine announcement; never
    invents one to fill the format.

    The headline's 26-character hard limit is a real, measured pixel-width
    budget, not a style preference — Text on a Face already established
    that DejaVuSans-Bold at 48px (the same font/size news_headline.py's own
    headline uses) needs roughly this budget to guarantee a single line at
    a ~968px plate width (that module's own docstring: a 35-char line
    already measured ~1252px, well past the plate). Live-confirmed here
    too: an ungapped 'Admissions close 14 September' (30 chars) wrapped to
    2 lines and overflowed the fixed lower-third bar zone in a real render."""
    if not (description or "").strip():
        return None
    prompt = (
        f"This is for a Nigerian ad for {_business_line(business_name, category, '', brand_context)}\n\n"
        "Below is that business's own description/context text:\n\n"
        f"{description}\n\n"
        "Does this text state a REAL, specific announcement worth leading with as news — "
        "a new branch opening, an admissions deadline, an event date, a genuine milestone, or "
        "anything else concrete? This format is photojournalistic 'news' style: the headline "
        "must state something that actually happened or is happening, never invented sentiment "
        "or a generic promotional claim.\n"
        "If nothing concrete is stated, return headline as an empty string — do not invent one.\n"
        "- headline: WHAT happened, stated plainly, NEVER a 'Breaking News'-style label. HARD "
        "LIMIT: 26 characters or fewer, including spaces and punctuation — this is a real "
        "pixel-width constraint, not a style preference. Correctly-sized examples: 'New Yaba "
        "branch now open' (25 chars), 'Term 2 admissions open' (23 chars).\n"
        "- secondary_line: one short supporting detail (<=35 characters), only if stated, else "
        "empty string — must NOT restate the date/timeframe (that belongs in date_stamp only)\n"
        "- date_stamp: WHEN it happens/closes (<=15 characters), only if a real date/timeframe "
        "is stated, else empty string. A live-confirmed real failure: headline omitted the date, "
        "secondary_line said 'Opening date is 1 October', AND date_stamp separately said "
        "'1 October' — the same date rendered twice, once spelled out and once bare. The date "
        "must appear in EXACTLY ONE of headline, secondary_line, or date_stamp — never in two of "
        "them, and never in all three.\n"
        "- announcement_subject: a short, concrete VISUAL scene for a documentary photo that "
        "could ONLY be this specific announcement for this specific business — never a scene "
        "generic enough to belong to any business on any day. Reason it out fresh each time: "
        "what does THIS particular news actually look like, physically, for a business that "
        "does THIS? A milestone (e.g. a customer-count achievement) does not look like a branch "
        "opening, which does not look like an admissions deadline, which does not look like a "
        "generic confident portrait — each is a genuinely different physical scene. Two "
        "live-confirmed real failures this must avoid: 'a woman working on a laptop in a store' "
        "for a branch opening (shows nothing about an opening), and 'a confident businesswoman, "
        "arms crossed, in a shop' for a customer-milestone announcement (a generic default pose "
        "reused regardless of what the news actually was — the milestone itself never appeared "
        "anywhere in the scene). No brand/person names.\n"
        f"- nigerian_setting: pick the single best-fitting option, copied EXACTLY, from this list: "
        f"{list(_NIGERIAN_SETTINGS)}\n"
        f"{('CORRECTION: ' + correction) if correction else ''}\n"
        "Return ONLY the JSON with exactly these 5 keys."
    )
    d = await _call_content_model(prompt)
    if not d or not str(d.get("headline", "")).strip():
        return None
    subject = str(d.get("announcement_subject", "")).strip()
    if not subject:
        return None
    setting = str(d.get("nigerian_setting", "")).strip()
    if setting not in _NIGERIAN_SETTINGS:
        setting = _NIGERIAN_SETTINGS[0]
    headline = str(d.get("headline", "")).strip()
    secondary_line = str(d.get("secondary_line", "")).strip() or None
    date_stamp = str(d.get("date_stamp", "")).strip() or None
    if not _news_grounded(headline, description):
        print(f"[VSG01] news headline not in the client's words, format skipped: {headline!r}", flush=True)
        return None
    # The supporting line and the date are claims too: drop one that isn't the client's.
    if secondary_line and not _news_grounded(secondary_line, description):
        secondary_line = None
    if date_stamp and not all(n in description for n in re.findall(r"\d+", date_stamp)):
        date_stamp = None
    # Belt-and-suspenders, not just a prompt instruction: when the only real
    # date in the source text already sits inside the headline (a single-
    # date announcement, the common case), the model tends to restate it in
    # date_stamp too regardless of being told not to — live-confirmed
    # ("Admissions close 30 Sept" + date_stamp "30 September" both shipped
    # in the same render despite the prompt's explicit instruction). Drop
    # date_stamp outright whenever any of its own words already appear in
    # the headline, rather than trusting instruction-following alone. Same
    # live-confirmed failure mode against secondary_line too — a real render
    # shipped secondary_line "Opening date is 1 October" AND date_stamp
    # "1 October" together, the same date spelled out twice.
    if date_stamp:
        headline_words = set(re.findall(r"[a-z0-9]+", headline.lower()))
        date_words = set(re.findall(r"[a-z0-9]+", date_stamp.lower()))
        if date_words & headline_words:
            date_stamp = None
    if date_stamp and secondary_line:
        secondary_words = set(re.findall(r"[a-z0-9]+", secondary_line.lower()))
        date_words = set(re.findall(r"[a-z0-9]+", date_stamp.lower()))
        if date_words & secondary_words:
            date_stamp = None
    return {
        "headline": headline,
        "secondary_line": secondary_line,
        "date_stamp": date_stamp,
        "announcement_subject": subject,
        "nigerian_setting": setting,
    }


async def _build_news_headline(business_name: str, category: str, description: str, tokens: dict,
                               photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                               brand_context: Optional[dict] = None):
    content = await _content_news_headline(business_name, category, description, brand_context=brand_context)
    if not content:
        return None
    from .ad_formats import news_headline
    width, height = _CANVAS_SIZE

    async def _gen_passing_skin_check(prompt: str) -> Optional[str]:
        """Same corrective-retry pattern as Problem/Solution's own helper
        (§1.7) — a real photojournalistic scene is likely to include a
        person, so this format needs the same skin-tone gate."""
        current_prompt = prompt
        for attempt in (1, 2):
            url = await generate_scene(current_prompt, size=f"{width}x{height}", brand_context=brand_context)
            result = await verify_skin_rendering(url)
            if not result["contains_person"] or result["matches_target_range"]:
                return url
            observed = result.get("skin_tone_observed") or "too light"
            print(f"[VSG01] News Headline scene failed skin-tone check "
                  f"(attempt {attempt}/2): {result['notes']}", flush=True)
            current_prompt = (
                f"CRITICAL: skin must be deep brown to dark brown, NOT {observed} "
                f"as last time. {prompt}"
            )
        return None

    try:
        scene_url = photo_url or await _gen_passing_skin_check(
            news_headline._scene_prompt(content["announcement_subject"], content["nigerian_setting"]),
        )
    except SceneGenerationFailed as e:
        print(f"[VSG01] News Headline scene generation failed: {e}", flush=True)
        return None
    if not scene_url:
        return None

    def _try_build(c: dict):
        return news_headline.build_document(
            scene_url, c["headline"], secondary_line=c["secondary_line"],
            date_stamp=c["date_stamp"], canvas_size=_CANVAS_SIZE, tokens=tokens,
            show_breaking_news_banner=True,
        )

    try:
        return _try_build(content)
    except news_headline.ContentOverflowsZone as e:
        # Same "regenerate once with the exact correction, then accept
        # whatever comes back" pattern as Text on a Face's own length
        # retry — the 26-char headline cap above should make this rare,
        # not eliminate it outright (a long secondary_line/date_stamp
        # stacked with a full-length headline can still overflow).
        print(f"[VSG01] News Headline content overflowed its zone, retrying shorter: {e}", flush=True)
        retry_content = await _content_news_headline(
            business_name, category, description,
            correction=f"your last attempt overflowed its fixed text zone ({e}). "
                       "Make the headline and any secondary_line/date_stamp shorter this time.",
            brand_context=brand_context,
        )
        if not retry_content:
            return None
        try:
            return _try_build(retry_content)
        except Exception as e2:
            print(f"[VSG01] News Headline still failed after retry: {e2}", flush=True)
            return None
    except Exception as e:
        print(f"[VSG01] News Headline build failed: {e}", flush=True)
        return None


# ── SEED-089: Humour / Cartoon ────────────────────────────────────────────

async def _content_humour_cartoon(business_name: str, category: str, description: str,
                                  brand_context: Optional[dict] = None) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, invent a "
        "single-panel cartoon sight gag about a SHARED SITUATION this business's customers "
        "would recognise (e.g. waiting forever for slow delivery, a messy DIY repair before "
        "calling a professional). The joke must target the SITUATION, never a group, "
        "ethnicity, region, or religion — no stereotypes.\n"
        "- situation: a short, concrete VISUAL gag description for a cartoon illustration — "
        "the punchline must be visible in the picture itself, not need a caption\n"
        f"- nigerian_setting: pick the single best-fitting option, copied EXACTLY, from this list: "
        f"{list(_NIGERIAN_SETTINGS)}\n"
        "Return ONLY the JSON with exactly these 2 keys."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    situation = str(d.get("situation", "")).strip()
    if not situation:
        return None
    setting = str(d.get("nigerian_setting", "")).strip()
    if setting not in _NIGERIAN_SETTINGS:
        setting = _NIGERIAN_SETTINGS[0]
    return {"situation": situation, "nigerian_setting": setting}


async def _build_humour_cartoon(business_name: str, category: str, description: str, tokens: dict,
                                photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                human_reviewed: bool = False, brand_context: Optional[dict] = None):
    """§2.12: 'Needs human review before shipping on the ₦15k tier, where no
    operator sees the asset first.' No async generate-hold-approve-resume
    workflow exists in this codebase (see module docstring) — rather than
    working around that with a hardcoded True, the real automatic
    generation path (which never passes human_reviewed) simply never gets
    a document from this builder, matching §2.12's actual requirement
    instead of a data flag pretending to satisfy it. human_reviewed=True is
    only ever passed by a genuinely human-supervised call site (e.g. a
    manual QA render, never `select_and_render_vsg01_creative`)."""
    if not human_reviewed:
        return None
    content = await _content_humour_cartoon(business_name, category, description, brand_context)
    if not content:
        return None
    from .ad_formats import humour_cartoon
    width, height = _CANVAS_SIZE
    try:
        illustration_url = await generate_scene(
            humour_cartoon._illustration_prompt(content["situation"], content["nigerian_setting"]),
            size=f"{width}x{height}", brand_context=brand_context,
        )
    except SceneGenerationFailed as e:
        print(f"[VSG01] Humour/Cartoon illustration generation failed: {e}", flush=True)
        return None
    try:
        document = humour_cartoon.build_document(
            illustration_url, human_reviewed=True, canvas_size=_CANVAS_SIZE, tokens=tokens,
        )
    except Exception as e:
        print(f"[VSG01] Humour/Cartoon build failed: {e}", flush=True)
        return None
    return document


# ── SEED-083: The Censored Item (needs a real, attested product photo) ───

async def _content_censored_item(business_name: str, category: str, description: str) -> Optional[dict]:
    """§2.10: 'There is a real pending reveal.' Same verbatim-fact contract
    as _content_receipt/_content_price_led_offer — only ever fires when the
    business's own words already state a genuine reveal date or mechanism;
    never invents one to fill the format."""
    if not (description or "").strip():
        return None
    prompt = (
        f"Below is a business's own description/context text:\n\n{description}\n\n"
        "Does this text state a REAL pending reveal — something specific being kept "
        "back until a stated date or mechanism (e.g. 'full menu revealed 1 October', "
        "'unboxed live on Friday')? This format only exists to build anticipation for "
        "an ACTUAL upcoming reveal, never to imply withheld shocking content.\n"
        "If no real reveal date/mechanism is stated, return reveal_text as an empty "
        "string — do not invent one.\n"
        "- reveal_text: the reveal date or mechanism, stated plainly (<=40 characters)\n"
        "- what_is_obscured: a short, neutral description of what's hidden (e.g. 'the new "
        "lid design') — MUST NOT be a price, cost, or amount; this format never obscures a "
        "price\n"
        "Return ONLY the JSON with exactly these 2 keys."
    )
    d = await _call_content_model(prompt)
    if not d or not str(d.get("reveal_text", "")).strip():
        return None
    what = str(d.get("what_is_obscured", "")).strip()
    if not what:
        return None
    return {"reveal_text": str(d.get("reveal_text", "")).strip(), "what_is_obscured": what}


async def _locate_reveal_region(photo_url: str, zone_width: int, zone_height: int) -> Optional[tuple]:
    """Vision-based redaction placement — same GPT-4o-mini vision-call
    pattern as skin_tone_check.py's own verify_skin_rendering (JSON-only
    response, temperature=0). This module has no way to locate 'the
    interesting part' of an arbitrary photo from text alone, but a vision
    model looking at the actual pixels can give a genuinely informed
    answer instead of a blind guess. Returns (x, y, width, height) in real
    pixel coordinates within the photo zone, or None on any failure/
    implausible answer — the caller falls back to a generic centred box,
    same fail-safe contract as every vision call in this codebase."""
    import json as _json
    from app.services.AIService import AIService

    prompt = (
        "This real product photo will be used for a 'coming soon' reveal ad. "
        "Identify ONE rectangular region of the image worth redacting to build "
        "anticipation — a distinguishing label, logo, or unique design detail "
        "of the product itself. Never a region containing any price or text. "
        "The region should sit entirely within the photo (not touching the "
        "very edges) and cover roughly 25-45% of the image's width and "
        "15-30% of its height.\n"
        "Return JSON only: {\"x\": 0.0-1.0, \"y\": 0.0-1.0, \"width\": 0.0-1.0, "
        "\"height\": 0.0-1.0} as fractions of the image's own width/height. "
        "Return ONLY the JSON, no markdown."
    )
    try:
        ai_request = AIService.build_ai_model(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": photo_url}},
                    {"type": "text", "text": prompt},
                ],
            }],
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=150,
        )
        ai_response = await AIService.chat_completion(ai_request)
        if isinstance(ai_response, dict) and "error" in ai_response:
            raise Exception(ai_response["error"])
        raw = ai_response.choices[0].message.content.strip()
        print(f"[VSG01] Censored Item reveal-region raw response: {raw!r}", flush=True)
        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.split("\n") if not line.startswith("```"))
        result = _json.loads(raw)
        fx, fy, fw, fh = (float(result[k]) for k in ("x", "y", "width", "height"))
        if not all(0.0 <= v <= 1.0 for v in (fx, fy, fw, fh)) or fx + fw > 1.0 or fy + fh > 1.0:
            print(f"[VSG01] Censored Item reveal-region implausible fractions "
                  f"x={fx} y={fy} w={fw} h={fh}, falling back to default", flush=True)
            return None
        x, y, w, h = int(fx * zone_width), int(fy * zone_height), int(fw * zone_width), int(fh * zone_height)
        if w < 20 or h < 20:
            print(f"[VSG01] Censored Item reveal-region too small w={w} h={h}, falling back to default", flush=True)
            return None
        print(f"[VSG01] Censored Item reveal-region located at x={x} y={y} w={w} h={h}", flush=True)
        return (x, y, w, h)
    except Exception as e:
        print(f"[VSG01] Censored Item reveal-region detection failed: {e}", flush=True)
        return None


async def _build_censored_item(business_name: str, category: str, description: str, tokens: dict,
                               photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                               obscure_box: Optional[tuple] = None,
                               brand_context: Optional[dict] = None):
    """§2.10: 'The obscured item must be the real product' — this builder
    only ever places the caller's own real photo_url, never a generated one
    (see censored_item.py's own module docstring on why `generate` is never
    permitted for the product itself). brand_context accepted (uniform
    builder signature) but unused: same verbatim-fact contract as Receipt/
    Price-Led Offer.

    obscure_box: (x, y, width, height) of the redaction bar, in real pixel
    coordinates. Explicit callers may still pass one directly; the normal
    path instead asks a vision model to locate a genuinely informed region
    on the actual photo (_locate_reveal_region), falling back to a generic
    centred box over roughly the middle third of the photo zone only if
    that call fails — a real, pixel-aware placement rather than a blind
    guess, but still not infallible, so the fallback stays as a safety
    net."""
    if not photo_url:
        return None
    content = await _content_censored_item(business_name, category, description)
    if not content:
        return None
    from .ad_formats import censored_item
    width, height = _CANVAS_SIZE
    photo_zone_height = int(height * 0.78)
    if obscure_box is None:
        obscure_box = await _locate_reveal_region(photo_url, width, photo_zone_height)
    if obscure_box is None:
        box_w, box_h = int(width * 0.4), int(photo_zone_height * 0.25)
        obscure_box = ((width - box_w) // 2, (photo_zone_height - box_h) // 2, box_w, box_h)
    try:
        document = censored_item.build_document(
            photo_url, *obscure_box, content["reveal_text"], content["what_is_obscured"],
            canvas_size=_CANVAS_SIZE, tokens=tokens,
        )
    except Exception as e:
        print(f"[VSG01] Censored Item build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Censored Item failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-078: Day 1 -> Day 30 (needs two real, caller-supplied photos) ───

async def _content_day1_day30(business_name: str, category: str, description: str,
                              brand_context: Optional[dict] = None) -> Optional[dict]:
    """§2.9: excluded outright for health/weight/skin/appearance — picks
    only from day1_day30.py's own closed allowlist, and only when the
    business's own words actually support that category (never defaults
    to one silently)."""
    from .ad_formats.day1_day30 import _PERMITTED_CATEGORIES
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}, does this "
        "business's work fit one of these EXACT categories: "
        f"{sorted(_PERMITTED_CATEGORIES)}? This format shows real progress over time — it is "
        "STRICTLY NEVER permitted for anything about a person's body, weight, skin, or "
        "appearance, regardless of how the business describes itself.\n"
        "If none of the listed categories genuinely fits, return category as an empty string.\n"
        "- category: copied EXACTLY from the list above, or empty string\n"
        "- day1_label: a short label for the earlier photo (<=12 characters), e.g. 'Day 1' — "
        "plain and factual, never a health/appearance claim\n"
        "- day30_label: a short label for the later photo (<=12 characters), e.g. 'Day 30'\n"
        "Return ONLY the JSON with exactly these 3 keys."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    category_pick = str(d.get("category", "")).strip()
    if category_pick not in _PERMITTED_CATEGORIES:
        return None
    return {
        "category": category_pick,
        "day1_label": str(d.get("day1_label", "")).strip() or "Day 1",
        "day30_label": str(d.get("day30_label", "")).strip() or "Day 30",
    }


async def _build_day1_day30(business_name: str, category: str, description: str, tokens: dict,
                            photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                            day30_photo_url: Optional[str] = None,
                            brand_context: Optional[dict] = None):
    """Both photo_url (day 1) and day30_photo_url (day 30) must be real,
    caller-supplied photos of the SAME real thing at two points in time —
    this builder never generates either (§1.2/§2.9's own stronger
    'excluded outright... never generate a synthetic transformation').
    There is no current attestation flow that produces a genuine day30_photo_url
    (only single product_photo/real_customer_photo attestations exist — see
    day1_day30.py's own module docstring), so this never actually receives
    one through the real automatic generation path today; it's real,
    tested code, ready for whenever that attestation exists, callable
    directly today for manual/QA rendering."""
    if not photo_url or not day30_photo_url:
        return None
    content = await _content_day1_day30(business_name, category, description, brand_context)
    if not content:
        return None
    from .ad_formats import day1_day30
    try:
        document = day1_day30.build_document(
            photo_url, day30_photo_url, content["category"],
            day1_label=content["day1_label"], day30_label=content["day30_label"],
            canvas_size=_CANVAS_SIZE, tokens=tokens,
        )
    except Exception as e:
        print(f"[VSG01] Day 1 -> Day 30 build failed: {e}", flush=True)
        return None
    # day1_day30.build_document doesn't self-check legibility (unlike most
    # other format modules — see that module's own code) — external check
    # here, same pattern as Us vs Them/Receipt/Review Card.
    if check_legibility(document, tokens):
        print("[VSG01] Day 1 -> Day 30 failed legibility check, falling back", flush=True)
        return None
    return document


# ── Poster formats: SEED-099 Event Promo, SEED-100 Offer Spotlight, SEED-101 Problems vs Solution ──
# Same contract as every format above: copy comes from the business's own words, any figure not
# in them is removed, and a builder returns None (falls through to the next format) rather than
# shipping something it can't stand behind.

def _figures_in(text: str) -> list:
    return re.findall(r"₦\s?[\d,.]+[kKmM]?|\d[\d,.]*\s?%|\d[\d,.]*", text or "")


def _drop_invented_figures(text: str, source: str) -> str:
    """Remove any ₦ amount, percentage or number that isn't in the business's own text."""
    src_digits = re.sub(r"[^\d]", "", source or "")
    for tok in _figures_in(text):
        digits = re.sub(r"[^\d]", "", tok)
        if digits and digits not in src_digits:
            text = re.sub(r"\s{2,}", " ", text.replace(tok, "")).strip(" ,.-")
    return text


def _brand_source(description: str, brand_context: Optional[dict]) -> str:
    """Everything the business itself has told us — what copy may be grounded in."""
    bc = brand_context or {}
    bits = [description or ""]
    for key in ("key_products_services", "unique_selling_proposition", "website", "tagline",
                "customer_pain_points", "customer_frustrations", "proof_points", "price_range"):
        v = bc.get(key)
        if isinstance(v, list):
            bits.extend(str(x) for x in v)
        elif v:
            bits.append(str(v))
    return "\n".join(bits)


async def _cutout(photo_url: str) -> Optional[str]:
    """The real photo with its background removed — remove.bg, then the local model. Never the
    image-model method, which redraws the picture (a real person's face or a real product must
    stay exactly as photographed)."""
    from app.utils.background_removal import remove_background
    for method in ("removebg", "rembg"):
        try:
            url = await remove_background(photo_url, method=method)
            # remove_background hands back the original photo when it couldn't remove
            # anything — that is not a cut-out, and pasting it as one shows a rectangle.
            if url and url != photo_url:
                return url
        except Exception as e:
            print(f"[VSG01] cutout via {method} failed: {e}", flush=True)
    return None


def _poster_style(brand_context: Optional[dict], tokens: dict, default_font: str) -> dict:
    from .ad_formats._type import brand_block_color, headline_family
    return {
        "headline_font": headline_family(brand_context, default_font),
        "brand_color": brand_block_color((brand_context or {}).get("brand_colors"), tokens),
    }


async def _content_event_promo(business_name: str, category: str, description: str,
                               brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}:\n"
        "Does the text announce a REAL upcoming event with a stated date — a webinar, class, "
        "workshop, challenge, masterclass, training, launch, meetup or similar? If not, return "
        "{\"is_event\": false}.\n"
        "If it does, return JSON with:\n"
        "- is_event: true\n"
        "- title: the event's own name if the text gives one, otherwise a short punchy name (<=6 "
        "words) built only from what the event is about\n"
        "- event_kind: what kind of event it is, <=5 words, using only details in the text "
        "(e.g. '5-Day WhatsApp Challenge', 'Free live webinar on Zoom')\n"
        "- date_text: the date EXACTLY as written in the text\n"
        "- time_text: the time exactly as written, or \"\"\n"
        "- host_name: the host's name only if the text names one, else \"\"\n"
        "- host_role: the host's title only if the text states it, else \"\"\n"
        "- action_line: <=3 words, e.g. 'Register now', 'Book your seat'. Say 'free' ONLY if the "
        "text says the event is free.\n"
        "- contact_line: a website, @handle or phone number ONLY if it appears in the text, else \"\"\n"
        "Never invent a date, price, speaker or link. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not d.get("is_event"):
        return None
    source = _brand_source(description, brand_context)
    date_text = str(d.get("date_text", "")).strip()
    if not date_text or not _appears_in(date_text, description):
        return None
    time_text = str(d.get("time_text", "")).strip()
    if time_text and _appears_in(time_text, description):
        date_text = f"{date_text}, {time_text}"
    host_name = str(d.get("host_name", "")).strip()
    if host_name and not _appears_in(host_name, description):
        host_name = ""
    host_role = str(d.get("host_role", "")).strip() if host_name else ""
    if host_role and not _appears_in(host_role, description):
        host_role = ""
    action = str(d.get("action_line", "")).strip()
    if "free" in action.lower() and "free" not in (description or "").lower():
        action = "Register now"
    contact = str(d.get("contact_line", "")).strip()
    if contact and not _appears_in(contact, source):
        contact = ""
    title = _drop_invented_figures(str(d.get("title", "")).strip(), description)
    kind = _drop_invented_figures(str(d.get("event_kind", "")).strip(), description)
    if not title or not kind:
        return None
    return {"title": title, "event_kind": kind, "date_text": date_text, "host_name": host_name or None,
            "host_role": host_role or None, "action_line": action or "Register now",
            "contact_line": contact or None}


async def _build_event_promo(business_name: str, category: str, description: str, tokens: dict,
                             photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                             brand_context: Optional[dict] = None):
    from .ad_formats import event_promo
    content = await _content_event_promo(business_name, category, description, brand_context)
    if not content:
        return None
    # photo_url here is always the owner's own attested photo — a customer photo or a product
    # photo never reaches this format (see _FORMATS_NOT_FOR_PHOTO).
    cutout = await _cutout(photo_url) if photo_url else None
    style = _poster_style(brand_context, tokens, event_promo.DEFAULT_FAMILY)
    scene_url = scene_surface = None
    if not photo_url:
        # No host photo: generated artwork about the event's topic fills the right side.
        # If it can't be made, the date-card version is used instead.
        try:
            scene_url = await generate_scene(
                event_promo.scene_prompt(content["title"], content["event_kind"], style["brand_color"]),
                size=f"{_CANVAS_SIZE[0]}x{_CANVAS_SIZE[1]}", brand_context=brand_context,
            )
            scene_surface = await _left_edge_colour(scene_url)
        except Exception as e:
            print(f"[VSG01] Event Promo artwork failed, using the date card: {e}", flush=True)
            scene_url = scene_surface = None
    contact, platforms = _event_contact(content["contact_line"], brand_context)
    builds = [scene_url, None] if scene_url else [None]
    for scene in builds:
        try:
            return event_promo.build_document(
                content["title"], content["event_kind"], content["date_text"],
                canvas_size=_CANVAS_SIZE, tokens=tokens,
                host_cutout_url=cutout, host_photo_url=None if cutout else photo_url,
                scene_url=scene, scene_surface=scene_surface if scene else None,
                host_name=content["host_name"], host_role=content["host_role"],
                action_line=content["action_line"], contact_line=contact, social_platforms=platforms,
                brand_logo_url=brand_logo_url, **style,
            )
        except Exception as e:
            print(f"[VSG01] Event Promo build failed{' with artwork' if scene else ''}: {e}", flush=True)
    return None


def _event_contact(contact: Optional[str], brand_context: Optional[dict]) -> tuple:
    """(contact line, networks to show icons for). The brief's own contact wins; an @handle in it
    gets the icons of the connected networks that handle is really on. Without one, the brand's
    own connected handle and its networks, then its website. An icon is never shown for a
    network the handle isn't on."""
    handles = (brand_context or {}).get("_social_handles") or []
    if contact:
        if contact.strip().startswith("@"):
            name = contact.strip().lstrip("@").lower()
            return contact, [h["platform"] for h in handles if str(h.get("username", "")).lstrip("@").lower() == name]
        return contact, []
    handle, platforms = _brand_handle(brand_context)
    if handle:
        return handle, platforms
    return (_brand_website(brand_context) or None), []


async def _left_edge_colour(url: str) -> Optional[str]:
    """The artwork's own background colour (the median of its calm left strip), so the text
    side and the picture read as one image. Hex light enough for dark text, else None."""
    import io as _io
    import httpx
    from PIL import Image, ImageStat
    from .ad_formats.brand_tokens import contrast_ratio
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    im = Image.open(_io.BytesIO(resp.content)).convert("RGB")
    w, h = im.size
    r, g, b = (int(v) for v in ImageStat.Stat(im.crop((0, int(h * 0.15), int(w * 0.08), int(h * 0.85)))).median)
    hexc = "#{:02X}{:02X}{:02X}".format(r, g, b)
    return hexc if contrast_ratio("#141014", hexc) >= 12 else None


async def _content_offer_spotlight(business_name: str, category: str, description: str,
                                   brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip():
        return None
    services_hint = (brand_context or {}).get("key_products_services") or []
    if isinstance(services_hint, str):
        services_hint = [services_hint]
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}"
        f"{(' — services: ' + '; '.join(map(str, services_hint))) if services_hint else ''}:\n"
        "Does the text state a REAL offer — a discount, a price cut, a free extra, a bundle deal? "
        "If not, return {\"has_offer\": false}.\n"
        "If it does, return JSON with:\n"
        "- has_offer: true\n"
        "- headline: <=9 words leading with the offer, e.g. 'Get 50% off all of our creative services'\n"
        "- emphasis: the offer's key figure or phrase copied EXACTLY from your headline, <=3 words "
        "(e.g. '50%', 'Free delivery')\n"
        "- ticket: <=5 words restating the offer for a small tag, e.g. '50% off all services'\n"
        "- services: up to 6 of the business's own services or products, each <=3 words, ONLY ones "
        "named above\n"
        "- scene: a short visual scene of ONE person relaxed and pleased, in a setting that suits "
        "this business (no brand names, no readable screens)\n"
        "- occasion: 2-4 words naming what the offer celebrates ONLY if the text says (e.g. "
        "'Anniversary Discounted Offer', 'December Sale'), else \"\"\n"
        "Never invent a figure: any number must appear in the text above. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not d.get("has_offer"):
        return None
    headline = _drop_invented_figures(str(d.get("headline", "")).strip(), description)
    emphasis = str(d.get("emphasis", "")).strip()
    ticket = _drop_invented_figures(str(d.get("ticket", "")).strip(), description)
    scene = str(d.get("scene", "")).strip()
    if not headline or not scene:
        return None
    # The offer itself must be real: its figure is in the business's words, or it's a stated
    # non-numeric offer ("free delivery") that also appears there.
    if not emphasis or emphasis.lower() not in headline.lower():
        return None
    figure_digits = re.sub(r"[^\d]", "", emphasis)
    if figure_digits:
        if figure_digits not in re.sub(r"[^\d]", "", description):
            return None
    elif not _appears_in(emphasis, description):
        return None
    # Tags sit beside the offer, so they list only what the brief itself puts on offer — a
    # service from the brand profile that isn't in the deal would read as discounted too.
    brief = (description or "").lower()
    services = [str(x).strip() for x in (d.get("services") or []) if str(x).strip()]
    services = [x for x in services if x.lower() in brief][:6]
    if len(services) < 2:
        services = []
    # The ticket restates the offer; it may not widen it ("25% off all services" when only
    # three services are discounted). Every word in it must come from the brief.
    if ticket and not all(w in brief or w in ("off", "on") for w in re.findall(r"[a-z]{3,}", ticket.lower())):
        ticket = f"{emphasis} off" if "off" in headline.lower() and "off" not in emphasis.lower() else emphasis
    # The occasion tag only says what the brief says the offer is for.
    occasion = str(d.get("occasion", "")).strip()
    if occasion and not all(w in brief or w in ("offer", "discounted", "discount", "sale", "special")
                            for w in re.findall(r"[a-z]{3,}", occasion.lower())):
        occasion = ""
    return {"headline": headline, "emphasis": emphasis, "ticket": ticket or None,
            "services": services, "scene": scene, "occasion": occasion or None}


async def _gen_scene_skin_checked(prompt: str, size: str, brand_context: Optional[dict]) -> Optional[str]:
    """§1.7 — the same generate / check skin rendering / one corrected retry used by
    Problem / Solution."""
    current = prompt
    for attempt in (1, 2):
        url = await generate_scene(current, size=size, brand_context=brand_context)
        result = await verify_skin_rendering(url)
        if not result["contains_person"] or result["matches_target_range"]:
            return url
        observed = result.get("skin_tone_observed") or "too light"
        print(f"[VSG01] scene failed skin-tone check (attempt {attempt}/2): {result['notes']}", flush=True)
        current = f"CRITICAL: skin must be deep brown to dark brown, NOT {observed} as last time. {prompt}"
    return None


async def _build_offer_spotlight(business_name: str, category: str, description: str, tokens: dict,
                                 photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                 brand_context: Optional[dict] = None):
    from .ad_formats import offer_spotlight
    content = await _content_offer_spotlight(business_name, category, description, brand_context)
    if not content:
        return None
    width, height = _CANVAS_SIZE
    try:
        scene_url = await _gen_scene_skin_checked(
            offer_spotlight.scene_prompt(content["scene"]), f"{width}x{height}", brand_context,
        )
    except SceneGenerationFailed as e:
        print(f"[VSG01] Offer Spotlight scene generation failed: {e}", flush=True)
        return None
    if not scene_url:
        return None
    try:
        return offer_spotlight.build_document(
            scene_url, content["headline"], content["emphasis"], content["ticket"], content["services"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
            occasion=content.get("occasion"),
            handle=_brand_handle(brand_context)[0] or _brand_website(brand_context) or None,
            **_poster_style(brand_context, tokens, offer_spotlight.DEFAULT_FAMILY),
        )
    except Exception as e:
        print(f"[VSG01] Offer Spotlight build failed: {e}", flush=True)
        return None


async def _content_problems_vs_solution(business_name: str, category: str, description: str,
                                        brand_context: Optional[dict] = None) -> Optional[dict]:
    bc = brand_context or {}
    pains = bc.get("customer_pain_points") or bc.get("customer_frustrations") or []
    if isinstance(pains, str):
        pains = [pains]
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}"
        f"{(' — known customer frustrations: ' + '; '.join(map(str, pains))) if pains else ''}:\n"
        "Write a 'your problems | our solution' split ad.\n"
        "- problems: 4 or 5 everyday frustrations this business's customers have, each <=5 words, "
        "in the customer's own plain words (e.g. 'Dry, dull skin by noon', 'Counting stock by hand')\n"
        "- solutions: one concrete RESULT the customer gets for each problem, in the same order, each "
        "<=5 words (e.g. 'Know what's selling'). No slogans like 'Elevate your brand'.\n"
        "No numbers, prices or percentages unless that exact figure appears above. "
        "Return ONLY JSON: {\"problems\": [...], \"solutions\": [...]}."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    source = _brand_source(description, brand_context)
    problems = [_drop_invented_figures(str(x).strip(), source) for x in (d.get("problems") or [])]
    solutions = [_drop_invented_figures(str(x).strip(), source) for x in (d.get("solutions") or [])]
    problems = [p for p in problems if p][:5]
    solutions = [s for s in solutions if s][:5]
    if len(problems) < 3 or len(solutions) < 3:
        return None
    return {"problems": problems, "solutions": solutions}


async def _build_problems_vs_solution(business_name: str, category: str, description: str, tokens: dict,
                                      photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                      brand_context: Optional[dict] = None):
    from .ad_formats import problems_vs_solution
    content = await _content_problems_vs_solution(business_name, category, description, brand_context)
    if not content:
        return None
    # photo_url here is always an attested product photo (see _FORMATS_NOT_FOR_PHOTO). Without a
    # clean cut-out the product isn't shown — the outcomes version is used instead.
    cutout = await _cutout(photo_url) if photo_url else None
    if photo_url and not cutout:
        return None
    try:
        return problems_vs_solution.build_document(
            content["problems"], solutions=content["solutions"], product_cutout_url=cutout,
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
            **_poster_style(brand_context, tokens, problems_vs_solution.DEFAULT_FAMILY),
        )
    except Exception as e:
        print(f"[VSG01] Problems vs Solution build failed: {e}", flush=True)
        return None


# ── Poster formats, second set: SEED-102 Portrait + Headline, SEED-103 Floating Cards,
# SEED-104 Enrol / Sign-up, SEED-105 Billboard Statement, SEED-106 AI Answer ──
# Same contract: copy from the business's own words, no figure it didn't give, every contact
# detail its own, and None (fall through) rather than an ad we can't stand behind.

def _brand_website(brand_context: Optional[dict]) -> str:
    bc = brand_context or {}
    site = str(bc.get("website") or bc.get("default_link") or "").strip()
    site = re.sub(r"^https?://", "", site).rstrip("/")
    return re.sub(r"^www\.", "", site) if site else ""


def _brand_handle(brand_context: Optional[dict]) -> tuple:
    """("@handle", [platforms it's on]) from the brand's own connected accounts, or ("", [])."""
    handles = (brand_context or {}).get("_social_handles") or []
    by_name: dict = {}
    for h in handles:
        name = str(h.get("username") or "").strip().lstrip("@")
        if name:
            by_name.setdefault(name.lower(), (name, []))[1].append(h["platform"])
    if not by_name:
        return "", []
    # The handle on the most networks; Instagram's breaks a tie (it's what people search).
    name, platforms = max(by_name.values(), key=lambda v: (len(v[1]), "instagram" in v[1]))
    return f"@{name}", platforms


async def _load_social_handles(db, brand_context: Optional[dict]) -> list:
    """[{platform, username}] for the brand's active connected accounts — read once per ad so
    a poster can show the brand's real handle and the networks it's on."""
    bc = brand_context or {}
    if db is None:
        return []
    scope = {"brand_id": bc["brand_id"]} if bc.get("brand_id") else (
        {"user_id": bc["user_id"], "brand_id": {"$exists": False}} if bc.get("user_id") else None)
    if not scope:
        return []
    names = {"twitter": "x", "x": "x", "instagram": "instagram", "facebook": "facebook", "tiktok": "tiktok",
             "linkedin": "linkedin", "youtube": "youtube"}
    out = []
    try:
        async for doc in db["social_connections"].find(
            {**scope, "connection_status": {"$in": ["active", None]}}, {"_id": 0, "platform": 1, "username": 1},
        ).limit(20):
            platform = names.get(str(doc.get("platform") or "").lower())
            if platform and doc.get("username"):
                out.append({"platform": platform, "username": str(doc["username"])})
    except Exception as e:
        print(f"[VSG01] social handles lookup failed: {e}", flush=True)
    return out


_PHONE_RE = re.compile(r"(?:\+?234|0)[\s-]?[789][01]\d(?:[\s-]?\d){7}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _contacts_in(description: str, brand_context: Optional[dict]) -> list:
    """[(kind, value)] — a phone number and an email only if the business wrote them, else its
    saved WhatsApp number. Never invented."""
    text = description or ""
    out = []
    phone = _PHONE_RE.search(text)
    if phone:
        out.append(("phone", re.sub(r"\s+", " ", phone.group(0)).strip()))
    else:
        wa = str((brand_context or {}).get("_whatsapp_number") or "").strip()
        if wa:
            digits = re.sub(r"\D", "", wa)
            if digits.startswith("234") and len(digits) == 13:
                wa = f"+234 {digits[3:6]} {digits[6:9]} {digits[9:]}"
            out.append(("phone", wa))
    email = _EMAIL_RE.search(text)
    if email:
        out.append(("email", email.group(0).rstrip(".")))
    return out


async def _gen_person_scene(prompt: str, brand_context: Optional[dict]) -> Optional[str]:
    try:
        return await _gen_scene_skin_checked(prompt, f"{_CANVAS_SIZE[0]}x{_CANVAS_SIZE[1]}", brand_context)
    except SceneGenerationFailed as e:
        print(f"[VSG01] scene generation failed: {e}", flush=True)
        return None


def _clean(d: dict, key: str, source: str, limit: int = 120) -> str:
    """A copy field with any figure the business didn't give removed."""
    return _drop_invented_figures(str(d.get(key, "") or "").strip(), source)[:limit].strip()


async def _content_portrait_headline(business_name: str, category: str, description: str,
                                     brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}:\n"
        "Write a portrait poster: one smiling person fills the photo with a big headline across them.\n"
        "Return JSON with:\n"
        "- headline: 3-6 words, the business's promise in plain words (e.g. 'Fresh Crops for Healthy "
        "Living')\n"
        "- emphasis: ONE word copied exactly from your headline, the word to colour\n"
        "- description: one sentence, 10-18 words, saying what the business does for the customer, only "
        "from the text above\n"
        "- cta_lead: 2-4 words leading into the website, e.g. 'Shop from us at:', 'Book with us at:'\n"
        "- person: who is in the photo, as a generic description that suits this business (e.g. 'a "
        "Nigerian woman farmer in her thirties wearing a colourful head-wrap') — never a named person\n"
        "- holding: what they hold or wear that shows the business (no brand names, no logos)\n"
        "- setting: where they are, a few words\n"
        "No numbers, prices or percentages unless that exact figure appears above. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    source = _brand_source(description, brand_context)
    headline = _clean(d, "headline", source, 60)
    emphasis = str(d.get("emphasis", "")).strip()
    if not headline or len(headline.split()) > 7:
        return None
    if emphasis and emphasis.lower() not in headline.lower():
        emphasis = ""
    person, holding, setting = (str(d.get(k, "")).strip() for k in ("person", "holding", "setting"))
    if not person or not setting:
        return None
    return {"headline": headline, "emphasis": emphasis or None,
            "description": _clean(d, "description", source, 140) or None,
            "cta_lead": str(d.get("cta_lead", "")).strip()[:30] or None,
            "person": person, "holding": holding or "smiling warmly", "setting": setting}


async def _build_portrait_headline(business_name: str, category: str, description: str, tokens: dict,
                                   photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                   brand_context: Optional[dict] = None):
    from .ad_formats import portrait_headline
    from .ad_formats._type import brand_pair
    content = await _content_portrait_headline(business_name, category, description, brand_context)
    if not content:
        return None
    # photo_url here is only ever the owner's own attested photo (see _FORMATS_NOT_FOR_PERSON):
    # their real photo is used whole. Otherwise a person is generated for the situation.
    scene = photo_url or await _gen_person_scene(
        portrait_headline.scene_prompt(content["person"], content["holding"], content["setting"]), brand_context)
    if not scene:
        return None
    style = _poster_style(brand_context, tokens, portrait_headline.DEFAULT_FAMILY)
    main, second = brand_pair((brand_context or {}).get("brand_colors"), tokens)
    target = _brand_website(brand_context) or _brand_handle(brand_context)[0] or None
    try:
        return portrait_headline.build_document(
            scene, content["headline"], content["emphasis"], content["description"],
            content["cta_lead"] if target else None, target, canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url, brand_name=business_name, headline_font=style["headline_font"],
            brand_color=main, second_color=second,
        )
    except Exception as e:
        print(f"[VSG01] Portrait + Headline build failed: {e}", flush=True)
        return None


async def _content_floating_cards(business_name: str, category: str, description: str,
                                  brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}:\n"
        "Write a lifestyle ad where two small app-style cards float over a photo of someone using the "
        "product or service, like pieces of its screen.\n"
        "Return JSON with:\n"
        "- headline: 4-8 words naming the customer's worry or the promise (e.g. 'Running out of stock "
        "shouldn't surprise you.')\n"
        "- subline: 3-8 words (e.g. 'Know what's selling, know what's left.')\n"
        "- card_one: {\"label\": 2-4 words, \"value\": 1-3 words} — something the product shows or does "
        "(e.g. {\"label\": \"Next appointment\", \"value\": \"Reminder sent\"})\n"
        "- card_two: {\"text\": 2-4 words, \"status\": 1-3 words} — a row with a status tag (e.g. "
        "{\"text\": \"Patient record saved\", \"status\": \"Synced\"})\n"
        f"- cta_line: 3-7 words ending with the business name, e.g. 'Track your inventory with "
        f"{business_name or 'us'}.'\n"
        "- person: a generic description of who is in the photo, suiting this business — never named\n"
        "- device: 'a laptop on her lap' or 'a phone in his hand' or similar\n"
        "- setting: where they are, a few words\n"
        "No numbers, prices or percentages anywhere unless that exact figure appears above. Return ONLY "
        "the JSON."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    source = _brand_source(description, brand_context)
    headline = _clean(d, "headline", source, 70)
    cta = _clean(d, "cta_line", source, 50)
    person, device, setting = (str(d.get(k, "")).strip() for k in ("person", "device", "setting"))
    if not headline or not cta or not person or not setting:
        return None
    c1, c2 = d.get("card_one") or {}, d.get("card_two") or {}
    card_one = (_clean(c1, "label", source, 28), _clean(c1, "value", source, 22)) if isinstance(c1, dict) else None
    card_two = (_clean(c2, "text", source, 30), _clean(c2, "status", source, 18)) if isinstance(c2, dict) else None
    return {"headline": headline, "subline": _clean(d, "subline", source, 60) or None,
            "card_one": card_one if card_one and all(card_one) else None,
            "card_two": card_two if card_two and card_two[0] else None,
            "cta_line": cta, "person": person, "device": device or "a laptop", "setting": setting}


async def _build_floating_cards(business_name: str, category: str, description: str, tokens: dict,
                                photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                brand_context: Optional[dict] = None):
    from .ad_formats import floating_cards
    from .ad_formats._type import brand_pair
    content = await _content_floating_cards(business_name, category, description, brand_context)
    if not content:
        return None
    scene = await _gen_person_scene(
        floating_cards.scene_prompt(content["person"], content["device"], content["setting"]), brand_context)
    if not scene:
        return None
    style = _poster_style(brand_context, tokens, floating_cards.DEFAULT_FAMILY)
    main, second = brand_pair((brand_context or {}).get("brand_colors"), tokens)
    try:
        return floating_cards.build_document(
            scene, content["headline"], content["subline"], content["card_one"], content["card_two"],
            content["cta_line"], _brand_website(brand_context) or None, canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url, headline_font=style["headline_font"], brand_color=main,
            second_color=second,
        )
    except Exception as e:
        print(f"[VSG01] Floating Cards build failed: {e}", flush=True)
        return None


async def _content_enrol_signup(business_name: str, category: str, description: str,
                                brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}:\n"
        "Is the text inviting people to ENROL, APPLY, REGISTER or SIGN UP — a school, course, programme, "
        "admission, cohort, class intake, membership or similar? If not, return {\"is_signup\": false}.\n"
        "If it is, return JSON with:\n"
        "- is_signup: true\n"
        "- kicker: 3-5 words above the headline (e.g. 'Your Future Begins Here')\n"
        "- headline: 2-5 words, bold, set in capitals (e.g. 'Campus, Career & Beyond')\n"
        "- side_line: 2-5 words beside the headline, or \"\"\n"
        "- button: 2-4 words, e.g. 'Secure your seat', 'Apply now', 'Join the next cohort'\n"
        "- person: a generic description of a happy learner or member who suits this business — never "
        "named\n"
        "- setting: where they are (e.g. 'a university campus', 'a bright training centre')\n"
        "- doing: what they're doing (e.g. 'with a laptop on her lap')\n"
        "Never invent a date, price, deadline or number. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d or not d.get("is_signup"):
        return None
    source = _brand_source(description, brand_context)
    headline = _clean(d, "headline", source, 40)
    button = _clean(d, "button", source, 28)
    person, setting = str(d.get("person", "")).strip(), str(d.get("setting", "")).strip()
    if not headline or not button or not person or not setting:
        return None
    return {"kicker": _clean(d, "kicker", source, 40) or None, "headline": headline,
            "side_line": _clean(d, "side_line", source, 36) or None, "button": button, "person": person,
            "setting": setting, "doing": str(d.get("doing", "")).strip() or "smiling"}


async def _build_enrol_signup(business_name: str, category: str, description: str, tokens: dict,
                              photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                              brand_context: Optional[dict] = None):
    from .ad_formats import enrol_signup
    content = await _content_enrol_signup(business_name, category, description, brand_context)
    if not content:
        return None
    scene = await _gen_person_scene(
        enrol_signup.scene_prompt(content["person"], content["setting"], content["doing"]), brand_context)
    if not scene:
        return None
    style = _poster_style(brand_context, tokens, enrol_signup.DEFAULT_FAMILY)
    try:
        return enrol_signup.build_document(
            scene, content["kicker"], content["headline"], content["side_line"], content["button"],
            _contacts_in(description, brand_context), _brand_website(brand_context) or None,
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url, **style,
        )
    except Exception as e:
        print(f"[VSG01] Enrol / Sign-up build failed: {e}", flush=True)
        return None


async def _content_billboard_statement(business_name: str, category: str, description: str,
                                       brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}:\n"
        "Write ONE bold billboard statement — a short, confident truth this business stands for, the "
        "kind that stops people on the road (e.g. 'Posting is not Marketing').\n"
        "Return JSON with:\n"
        "- statement: 3-6 words\n"
        "- highlight: 1-3 consecutive words copied exactly from the statement, the turn of the line "
        "(e.g. 'is not')\n"
        "- subline: 4-9 words that follow it up (e.g. 'Take your business to the next level.')\n"
        "- sub_emphasis: the last 1-3 words of the subline, copied exactly\n"
        "No numbers, prices, guarantees or claims about competitors, and nothing the text above doesn't "
        "support. Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    source = _brand_source(description, brand_context)
    statement = _clean(d, "statement", source, 48)
    if not statement or len(statement.split()) > 7:
        return None
    highlight = str(d.get("highlight", "")).strip()
    if highlight and highlight.lower() not in statement.lower():
        highlight = ""
    subline = _clean(d, "subline", source, 70)
    sub_emphasis = str(d.get("sub_emphasis", "")).strip()
    if sub_emphasis and sub_emphasis.lower() not in subline.lower():
        sub_emphasis = ""
    return {"statement": statement, "highlight": highlight or None, "subline": subline or None,
            "sub_emphasis": sub_emphasis or None}


async def _billboard_scene(panel_hex: str, brand_context: Optional[dict]):
    """(scene_url, panel box on the canvas, measured panel colour) — two tries, or None."""
    import io as _io
    import httpx
    from PIL import Image
    from .ad_formats import billboard_statement
    for attempt in (1, 2):
        try:
            url = await generate_scene(billboard_statement.scene_prompt(panel_hex),
                                       size=f"{_CANVAS_SIZE[0]}x{_CANVAS_SIZE[1]}", brand_context=None)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            im = Image.open(_io.BytesIO(resp.content)).convert("RGB").resize(_CANVAS_SIZE)
            found = billboard_statement.find_panel(im, panel_hex)
            if found:
                return url, found[0], found[1]
            print(f"[VSG01] Billboard: no clean blank face found (attempt {attempt}/2)", flush=True)
        except Exception as e:
            print(f"[VSG01] Billboard scene failed (attempt {attempt}/2): {e}", flush=True)
    return None


async def _build_billboard_statement(business_name: str, category: str, description: str, tokens: dict,
                                     photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                                     brand_context: Optional[dict] = None):
    from .ad_formats import billboard_statement
    from .ad_formats._type import brand_pair
    content = await _content_billboard_statement(business_name, category, description, brand_context)
    if not content:
        return None
    main, second = brand_pair((brand_context or {}).get("brand_colors"), tokens)
    found = await _billboard_scene(main, brand_context)
    if not found:
        return None
    scene, box, panel = found
    style = _poster_style(brand_context, tokens, billboard_statement.DEFAULT_FAMILY)
    try:
        return billboard_statement.build_document(
            scene, box, panel, content["statement"], content["highlight"], content["subline"],
            content["sub_emphasis"], canvas_size=_CANVAS_SIZE, tokens=tokens,
            website=_brand_website(brand_context) or None, headline_font=style["headline_font"],
            second_color=second if second.upper() != main.upper() else None,
        )
    except Exception as e:
        print(f"[VSG01] Billboard Statement build failed: {e}", flush=True)
        return None


async def _content_ai_answer(business_name: str, category: str, description: str,
                             brand_context: Optional[dict] = None) -> Optional[dict]:
    if not (description or "").strip() or not business_name:
        return None
    services = (brand_context or {}).get("key_products_services") or []
    if isinstance(services, str):
        services = [services]
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description, brand_context)}"
        f"{(' — services: ' + '; '.join(map(str, services))) if services else ''}:\n"
        "The ad looks like a chat with an AI assistant: a customer asks a real question, and the "
        "answer gives genuinely useful advice, then says what this business offers.\n"
        "Return JSON with:\n"
        "- question: the customer's question as they'd type it, casual and lowercase, 8-16 words "
        "(e.g. 'i'm a nigerian student with my course picked. what do i sort out next?')\n"
        "- advice: ONE sentence of real, general advice that answers it, 12-22 words, no business "
        "named\n"
        f"- offer: ONE plain sentence, 10-20 words, starting with '{business_name}', saying what it "
        "offers — only services named above, no praise, no 'best', no numbers\n"
        "Return ONLY the JSON."
    )
    d = await _call_content_model(prompt)
    if not d:
        return None
    source = _brand_source(description, brand_context)
    question = str(d.get("question", "")).strip()
    advice = _clean(d, "advice", source, 170)
    offer = _clean(d, "offer", source, 150)
    if not question or not advice or not offer or business_name.lower() not in offer.lower():
        return None
    if re.search(r"\b(best|number one|no\.? ?1|#1|guarantee)", offer.lower()):
        return None
    return {"question": question[:110], "advice": advice, "offer": offer}


async def _build_ai_answer(business_name: str, category: str, description: str, tokens: dict,
                           photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None,
                           brand_context: Optional[dict] = None):
    from .ad_formats import ai_answer
    content = await _content_ai_answer(business_name, category, description, brand_context)
    if not content:
        return None
    try:
        return ai_answer.build_document(content["question"], content["advice"], content["offer"],
                                        canvas_size=_CANVAS_SIZE, tokens=tokens)
    except Exception as e:
        print(f"[VSG01] AI Answer build failed: {e}", flush=True)
        return None


_BUILDERS = {
    "SEED-075": _build_us_vs_them,
    "SEED-087": _build_borrowed_interface,
    "SEED-080": _build_problem_solution,
    "SEED-081": _build_receipt,
    "SEED-093": _build_review_card,
    "SEED-082": _build_text_on_a_face,
    "SEED-074": _build_testimonial_offer,
    "SEED-088": _build_starter_pack,
    "SEED-096": _build_price_led_offer,
    "SEED-097": _build_text_only,
    "SEED-098": _build_work_in_progress,
    "SEED-077": _build_news_headline,
    "SEED-089": _build_humour_cartoon,
    "SEED-083": _build_censored_item,
    "SEED-078": _build_day1_day30,
    "SEED-099": _build_event_promo,
    "SEED-100": _build_offer_spotlight,
    "SEED-101": _build_problems_vs_solution,
    "SEED-102": _build_portrait_headline,
    "SEED-103": _build_floating_cards,
    "SEED-104": _build_enrol_signup,
    "SEED-105": _build_billboard_statement,
    "SEED-106": _build_ai_answer,
}


# format_id -> (has_person, person_is_real) for attribute tagging (§4) — a
# fact about the specific rendered asset, not derivable from the document
# itself (see attribute_tagging.py's own docstring). Text on a Face and
# Testimonial+Offer's person path both composite a real, attested customer
# photo; Review Card and Starter Pack show a product, not necessarily a
# person; the three no-photo/generated formats never depict a specific real
# person either.
_PERSON_ATTRIBUTES_BY_FORMAT = {
    "SEED-082": (True, True),
    "SEED-074": (True, True),
    # The second poster set shows a generated person (Portrait + Headline may also use the
    # owner's own photo, which this static table can't tell apart).
    "SEED-102": (True, False),
    "SEED-103": (True, False),
    "SEED-104": (True, False),
}


async def render_vsg01_creative(
    strategy: Strategy, business_name: str, category: str, description: str,
    brand_context: Optional[dict] = None, photo_url: Optional[str] = None,
    day30_photo_url: Optional[str] = None,
) -> Optional[dict]:
    """Build + render one selected format. Returns
    {"png_bytes": bytes, "format_id": str, "attributes": dict} on success,
    None on any failure (caller tries the next-ranked candidate, or falls
    back to generic generation) — never raises, matching every other
    creative.py entry point's own contract."""
    builder = _BUILDERS.get(strategy.strategy_id)
    if builder is None:
        return None
    format_def = FORMAT_MODULES[strategy.strategy_id].FORMAT
    tokens = resolve_brand_tokens((brand_context or {}).get("brand_colors"))
    # The user's actual real logo (Brand Playbook), never fabricated. Withheld
    # entirely for a brand_mark="prohibited" format (a logo actively damages
    # the mechanism there — §6.6/§6.8/§6.12) regardless of whether one is on
    # file; every other format gets it when the brand actually has one.
    brand_logo_url = (
        (brand_context or {}).get("logo_url") if format_def.brand_mark != "prohibited" else None
    )

    # Humour/Cartoon (SEED-089) hard-requires human_reviewed=True (§2.12: "no
    # operator sees the asset before it ships"). Every real path that reaches
    # this function generates a PREVIEW — creative.py's GENERATE/UPLOAD/
    # RECOMPOSITE sources all return a draft the business owner sees in chat;
    # actually publishing to Meta is a separate, explicit call
    # (POST /jane-ads/meta/plan/{id}/launch) the business owner triggers
    # themselves after seeing this exact asset. That IS a human reviewing it
    # before it ships — satisfied by this architecture, not bypassed.
    extra_kwargs: dict = {}
    if strategy.strategy_id == "SEED-089":
        extra_kwargs["human_reviewed"] = True
    if strategy.strategy_id == "SEED-078":
        extra_kwargs["day30_photo_url"] = day30_photo_url

    # brand_context now goes to every builder, not just the 5 that
    # AI-generate a scene: it's also how copy (headline, quote, offer text,
    # chat turns, etc.) gets voice-matched to the brand — same idea as
    # organic content's write_ad_copy, which is explicitly "voice-matched
    # to the brand playbook when a profile exists" (creative.py's own
    # docstring) and always has been, unlike this module's copy until now.
    # A builder that has no use for it (Receipt/Price-Led Offer/Censored
    # Item — genuinely verbatim-fact extraction, not styled prose; see each
    # _content_* function's own "never invents" contract) simply accepts
    # and ignores the param, same uniform-signature pattern tokens/
    # photo_url/brand_logo_url already use.
    document = await builder(
        business_name, category, description, tokens, photo_url=photo_url, brand_logo_url=brand_logo_url,
        brand_context=brand_context, **extra_kwargs,
    )
    if document is None:
        return None
    # The poster's standalone logo goes where the brand chose (Brand Playbook logo position/size).
    from .ad_formats.brand_logo import place_brand_logo
    document = place_brand_logo(document, brand_context)

    try:
        from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService
        png_bytes = await DocumentRendererService.render_to_png(document)
    except Exception as e:
        print(f"[VSG01] render_to_png failed for {strategy.strategy_id}: {e}", flush=True)
        return None

    has_person, person_is_real = _PERSON_ATTRIBUTES_BY_FORMAT.get(strategy.strategy_id, (False, False))
    attributes = build_ad_format_attributes(
        format_def, document, has_person=has_person, person_is_real=person_is_real,
    )
    return {"png_bytes": png_bytes, "format_id": strategy.strategy_id, "attributes": attributes}


PHOTO_ATTESTATIONS = ("product_photo", "real_customer_photo", "owner_photo")

# Whose words a person-photo format may put beside them. Text on a Face lays a line
# composed in the OWNER's voice across the face, so over a customer's photo it would
# put invented words in a real customer's mouth; Testimonial + Offer presents the
# person as a customer, which the owner or their staff are not.
_FORMATS_NOT_FOR_PERSON = {
    # Event Promo presents the person as the event's host and Problems vs Solution presents
    # the photo as the product, so neither may take a customer's photo.
    # Portrait + Headline lays the business's own headline across the person, so it takes the
    # owner's photo but never a customer's (the words would read as theirs) or a product photo.
    "real_customer_photo": frozenset({"SEED-082", "SEED-099", "SEED-101", "SEED-102"}),
    "owner_photo": frozenset({"SEED-074", "SEED-101"}),
    # A product photo is not a host, nor a portrait.
    "product_photo": frozenset({"SEED-099", "SEED-102"}),
}


def _vsg01_candidate_params(
    *, photo_url: Optional[str], photo_attestation: Optional[str], recomposite: bool,
) -> tuple[Optional[frozenset], bool, bool, Optional[str]]:
    """The exact candidate_ids/has_product_photo/has_real_customer_photo logic
    select_and_render_vsg01_creative already used inline — factored out so the
    pre-generation suggest-format endpoint (router.py's POST /jane-ads/creative/
    suggest-format) computes the SAME eligible pool a real generation call
    would, rather than a second hand-maintained copy that could drift."""
    if photo_url and photo_attestation in PHOTO_ATTESTATIONS:
        candidate_ids = RECOMPOSITE_PHOTO_FORMAT_IDS if recomposite else UPLOAD_PHOTO_FORMAT_IDS
        has_product_photo = photo_attestation == "product_photo"
        # Both are a real, consenting person — what differs is whose words a format
        # may put beside them (see _FORMATS_BY_PERSON).
        has_real_customer_photo = photo_attestation in ("real_customer_photo", "owner_photo")
        candidate_ids = candidate_ids - _FORMATS_NOT_FOR_PERSON.get(photo_attestation, frozenset())
        return candidate_ids, has_product_photo, has_real_customer_photo, photo_url
    return NO_PHOTO_FORMAT_IDS, False, False, None


def pick_preferred_ad_format(preferred: list, rotation_index: int, eligible_ids: set) -> tuple:
    """Next of the brand's preferred ad formats (Brand Playbook "Visual Styles —
    Ads") that's eligible for this ad, taking turns from rotation_index.

    Returns (format_id, next_rotation_index), or (None, None) when no preferred
    format is eligible — ranking then proceeds exactly as without a preference.
    """
    preferred = [p for p in preferred if p]
    if not preferred:
        return None, None
    try:
        start = int(rotation_index or 0) % len(preferred)
    except (TypeError, ValueError):
        start = 0
    for offset in range(len(preferred)):
        position = (start + offset) % len(preferred)
        if preferred[position] in eligible_ids:
            return preferred[position], (position + 1) % len(preferred)
    return None, None


async def select_and_render_vsg01_creative(
    db, business_name: str, category: str, description: str,
    brand_context: Optional[dict] = None, *,
    photo_url: Optional[str] = None,
    photo_attestation: Optional[str] = None,
    recomposite: bool = False,
    forced_format_id: Optional[str] = None,
    day30_photo_url: Optional[str] = None,
    exclude_format_ids: Optional[list[str]] = None,
) -> Optional[dict]:
    """The one call creative.py's GENERATE/UPLOAD/RECOMPOSITE paths need:
    select eligible formats from the real corpus (scoped to what this module
    can honestly finish given what it was called with), then try each
    ranked candidate's build+render in order until one succeeds. None means
    "use the existing generic image path" — exactly the fallback creative.py
    already has for a failed generic generation, so wiring this in changes
    nothing about the failure mode a caller has to handle.

    `photo_url` + `photo_attestation` ("product_photo" | "real_customer_photo")
    come from creative_from_upload/creative_from_recomposite once the user has
    confirmed what the photo actually is — omitted entirely (both None) for
    the GENERATE path, which has no real photo and must never claim one.
    `recomposite=True` additionally allows Starter Pack (needs a clean cutout
    — see module docstring); a plain upload does not.

    `forced_format_id` — the user picked a specific format from the
    alternatives shown by POST /jane-ads/creative/suggest-format (the "change"
    link next to the suggested style) for THIS one ad. If it's present in the
    SAME ranked/eligible list this function would have used anyway, it's tried
    first; otherwise (stale id, ineligible for this request) it's silently
    ignored and ranking proceeds normally — an override can never force a
    format the business isn't actually eligible for, and a bad override never
    breaks generation.

    When no per-request override is given, falls back to the brand's own
    standing preference — `brand_context["ad_format_selections"]` (set via the
    Brand Playbook's "Visual Styles — Ads" gallery, same idea as organic's
    `style_selections`) — using the FIRST selected format that's actually
    eligible for this request, same fail-open contract as forced_format_id.

    Precedence, highest first: an explicit `forced_format_id` > a standing
    Playbook preference > `description`'s own content-fit signal (Layer 2,
    see `_content_fit_boost`) > the ordered no-signal fallback list
    (`_DEFAULT_FORMAT_IDS`, only when NO content signal fired) > plain
    eligibility order (last resort). Each layer only reorders what the layer
    below it already found eligible — none of them can make an ineligible
    format render. And ranked #1 != rendered: `select_and_render_...` walks
    the list in order, skipping any pick whose content/build step comes up
    empty, so the fallback list's 2nd entry (Text-Only, always renders) is
    what actually catches a failed 1st entry instead of Us vs Them.
    """
    candidate_ids, has_product_photo, has_real_customer_photo, photo_url = _vsg01_candidate_params(
        photo_url=photo_url, photo_attestation=photo_attestation, recomposite=recomposite,
    )
    ranked = await select_ranked_ad_formats(
        db, has_product_photo=has_product_photo, has_real_customer_photo=has_real_customer_photo,
        isolated_ad_account=VSG01_ISOLATED_AD_ACCOUNT, candidate_ids=candidate_ids, description=description,
    )
    # Ads tested together must differ in structure, not just wording.
    ranked = [s for s in ranked if s.strategy_id not in (exclude_format_ids or [])]
    ranked_ids = {s.strategy_id for s in ranked}
    effective_forced = forced_format_id if forced_format_id in ranked_ids else None
    if effective_forced is None:
        effective_forced, next_rotation = pick_preferred_ad_format(
            (brand_context or {}).get("ad_format_selections") or [],
            (brand_context or {}).get("ad_format_rotation_index") or 0,
            ranked_ids,
        )
        scope = (brand_context or {}).get("_profile_scope")
        if effective_forced and next_rotation is not None and scope and db is not None:
            # Take turns through the Playbook's preferred ad formats instead of
            # always leading with the first one.
            try:
                await db["brand_profiles"].update_one(scope, {"$set": {"ad_format_rotation_index": next_rotation}})
            except Exception as exc:
                print(f"[VSG01] ad format rotation save failed: {exc}", flush=True)
    if effective_forced:
        ranked = (
            [s for s in ranked if s.strategy_id == effective_forced]
            + [s for s in ranked if s.strategy_id != effective_forced]
        )

    # The poster formats show the brand's real handle (with the networks it's on) and its saved
    # WhatsApp number — read once here, only when a ranked format can use them.
    ranked_now = {s.strategy_id for s in ranked}
    if db is not None and ranked_now & {"SEED-099", "SEED-100", "SEED-102", "SEED-104"}:
        extra = {}
        if "_social_handles" not in (brand_context or {}):
            extra["_social_handles"] = await _load_social_handles(db, brand_context)
        if "SEED-104" in ranked_now and "_whatsapp_number" not in (brand_context or {}):
            try:
                from .whatsapp import get_brand_whatsapp
                extra["_whatsapp_number"] = await get_brand_whatsapp(db, (brand_context or {}).get("brand_id"))
            except Exception as e:
                print(f"[VSG01] WhatsApp number lookup failed: {e}", flush=True)
        brand_context = {**(brand_context or {}), **extra}

    business_id = str((brand_context or {}).get("brand_id") or (brand_context or {}).get("user_id") or business_name)
    for strategy in ranked:
        format_def = FORMAT_MODULES[strategy.strategy_id].FORMAT
        if format_def.requires_isolation and db is not None:
            from .isolation_cap import IsolationCapService, MongoIsolationUsageStore
            cap_service = IsolationCapService(MongoIsolationUsageStore(db))
            decision = await cap_service.check(format_def)
            if not decision.allowed:
                print(f"[VSG01] {strategy.strategy_id} skipped: {decision.reason}", flush=True)
                continue
        result = await render_vsg01_creative(
            strategy, business_name, category, description, brand_context, photo_url=photo_url,
            day30_photo_url=day30_photo_url,
        )
        if result is not None:
            if format_def.requires_isolation and db is not None:
                await cap_service.record(format_def, business_id)
            return result
    return None
