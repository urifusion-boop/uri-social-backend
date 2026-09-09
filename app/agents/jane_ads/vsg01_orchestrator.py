"""
VSG-01 v3 — the format-selection + generation orchestrator (§6-9, step 10).

Every format module and every supporting primitive (brand_tokens, visual_slots,
legibility, skin_tone_check, isolation_cap, attribute_tagging) was built and
shipped with the same note in its own docstring: "not yet wired into a live
call path... this is the primitive that step calls." This module is that
step — the one place that actually calls them together, in order, around a
real render.

SCOPE. All 12 formats are real, retrievable corpus records (step 9) and
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

The remaining 5 formats (News Headline, Day 1->Day 30, The Censored Item,
Humour/Cartoon) are deliberately NOT auto-rendered — each for a real,
distinct reason, not a gap in this module (see the plan this was built from,
`~/.claude/plans/lovely-prancing-seahorse.md` at time of writing, for the
full reasoning):
  - News Headline / Day 1->Day 30 / The Censored Item all require
    `isolated_ad_account=True`. Grep confirms no per-brand ad account exists
    anywhere in this codebase — every brand advertises through the single
    global `settings.META_AD_ACCOUNT_ID`. This is a platform-level gap
    (per-brand Meta ad accounts, a separate infrastructure initiative), not
    something a BusinessProfile flag here can honestly satisfy.
  - Humour/Cartoon's own build_document HARD-BLOCKS on human_reviewed=True
    with no default — satisfying that honestly means an async
    generate-hold-approve-resume workflow this endpoint doesn't have, not a
    data flag.

Falls back to the existing generic image (`generate_ad_image`) at every
possible failure point — selection returning nothing, every candidate's
content step coming up empty, a built document failing its own legibility
check, a generated scene failing its skin-tone check, or the render call
itself failing. Never raises; `select_and_render_vsg01_creative` returns None
on any of these, exactly like `generate_ad_image` already does.
"""
from __future__ import annotations

import json
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
from .vsg01_corpus_seed import FORMAT_MODULES

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
})
UPLOAD_PHOTO_FORMAT_IDS = frozenset({"SEED-093", "SEED-082", "SEED-074", "SEED-096"})
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
# actually SAYS. This layer narrows among the already-eligible formats using
# real phrases the business itself wrote in `description` — it never makes
# an ineligible format eligible, and a format with no phrase match is never
# penalized, just left in its original (eligibility-score) order. Cheap,
# deterministic keyword matching on purpose, not an LLM classification call:
# no added latency/cost on every suggestion, and a keyword either is or
# isn't in the text — nothing here is inferred or invented from the words,
# matching this module's existing "never fabricate" contract for content.
_CONTENT_FIT_SIGNALS: dict[str, tuple[str, ...]] = {
    # Us vs Them — the brief itself frames a comparison.
    "SEED-075": (
        "vs ", "vs.", "versus", "unlike other", "unlike most", "compared to",
        "better than", "while other", "other salons", "other brands",
        "other shops", "not like other",
    ),
    # Price-Led Offer — a concrete price, discount, or markdown is stated.
    "SEED-096": (
        "% off", "percent off", "discount", "was ₦", "was $", "was £",
        "now ₦", "now $", "now only", "limited time price", "price drop",
        "slash", "half price",
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
    # Text-Only — an explicit single bold statement, no product/photo framing.
    "SEED-097": ("just says", "one line", "bold statement", "headline that reads"),
}


def _content_fit_boost(description: str) -> dict[str, float]:
    """Returns {format_id: 1.0} for every format whose signal phrases appear
    verbatim in the business's own stated brief — empty dict (no boost, no
    reorder) when nothing matches, which is the common case and the safe
    default."""
    text = (description or "").lower()
    return {fid: 1.0 for fid, phrases in _CONTENT_FIT_SIGNALS.items() if any(p in text for p in phrases)}


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
    fit = _content_fit_boost(description)
    if fit:
        records = sorted(records, key=lambda s: -fit.get(s.strategy_id, 0.0))
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
        return None


def _business_line(business_name: str, category: str, description: str) -> str:
    return f"'{business_name or 'a business'}' (a {category or 'local business'}){(' — ' + description) if description else ''}"


# ── SEED-075: Us vs Them ──────────────────────────────────────────────────

async def _content_us_vs_them(business_name: str, category: str, description: str) -> Optional[list]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, "
        "write 2-3 short comparison rows contrasting the OLD/informal way people currently "
        "handle this against how this business does it.\n"
        "HARD RULE: the 'them' side must name a generic METHOD ('buying at the market', "
        "'doing it yourself', 'guesswork', 'waiting days'), never a specific competitor or "
        "brand name — this is enforced downstream and a named business will be rejected.\n"
        "Return JSON: {\"rows\": [{\"label\": \"short row label e.g. Price\", "
        "\"them\": \"...\", \"us\": \"...\"}, ...]}. 2-3 rows only. Return ONLY the JSON."
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


async def _build_us_vs_them(business_name: str, category: str, description: str, tokens: dict,
                            photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    rows = await _content_us_vs_them(business_name, category, description)
    if not rows:
        return None
    try:
        document = us_vs_them.build_document(
            rows, canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Us vs Them build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Us vs Them failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-087: Borrowed Interface ──────────────────────────────────────────

async def _content_borrowed_interface(business_name: str, category: str, description: str) -> Optional[list]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, write a "
        "short, realistic WhatsApp-style exchange (3-4 messages total) between a customer and the "
        "business, ending with the business's offer or answer as the final message. Plausible "
        "casual Nigerian phrasing, no emoji spam.\n"
        "Return JSON: {\"turns\": [{\"speaker\": \"them\"|\"us\", \"message\": \"...\", "
        "\"timestamp\": \"e.g. 10:41 AM\"}, ...]}. 3-4 turns, last turn speaker must be \"us\". "
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
    return turns or None


async def _build_borrowed_interface(business_name: str, category: str, description: str, tokens: dict,
                                    photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    # brand_logo_url accepted (uniform call signature across every builder) but
    # deliberately never used — VSG-01-PROMPTS v2 §6.6: "A logo destroys this
    # format" (brand_mark="prohibited"). render_vsg01_creative never actually
    # passes one here (gated on format_def.brand_mark), so this is belt-and-
    # suspenders, not the real enforcement point.
    turns = await _content_borrowed_interface(business_name, category, description)
    if not turns:
        return None
    try:
        document = borrowed_interface.build_document(turns, canvas_size=_CANVAS_SIZE, tokens=tokens)
    except Exception as e:
        print(f"[VSG01] Borrowed Interface build failed: {e}", flush=True)
        return None
    if check_legibility(document, tokens):
        print("[VSG01] Borrowed Interface failed legibility check, falling back", flush=True)
        return None
    return document


# ── SEED-080: Problem / Solution ──────────────────────────────────────────

_NIGERIAN_SETTINGS = (
    "a Lagos street with informal shopfronts", "a small tiled shop interior",
    "an open-air market stall", "a compound courtyard", "a tailoring workshop",
    "a modern Lagos office interior", "a residential estate gate", "a roadside food stand",
)


async def _content_problem_solution(business_name: str, category: str, description: str) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, describe "
        "the PROBLEM this business solves and the SOLUTION it offers, for a two-zone visual ad.\n"
        "- problem_situation: a short, concrete VISUAL scene of the problem (what a camera would "
        "see, no people's names, no brand names, no location names)\n"
        "- solution_situation: a short, concrete VISUAL scene of the resolved state\n"
        "- problem_text: the problem stated as a naira cost/pain, <=8 words\n"
        "- solution_text: the solution stated as an outcome (not a feature), <=8 words\n"
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
    return out


async def _build_problem_solution(business_name: str, category: str, description: str, tokens: dict,
                                  photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    content = await _content_problem_solution(business_name, category, description)
    if not content:
        return None
    width, height = _CANVAS_SIZE
    zone_size = f"{width}x{height // 2}"
    try:
        problem_url = await generate_scene(
            problem_solution._problem_prompt(content["problem_situation"], content["nigerian_setting"]),
            size=zone_size,
        )
        solution_url = await generate_scene(
            problem_solution._solution_prompt(content["solution_situation"], content["nigerian_setting"]),
            size=zone_size,
        )
    except SceneGenerationFailed as e:
        print(f"[VSG01] Problem/Solution scene generation failed: {e}", flush=True)
        return None

    # §1.7 — verify skin rendering on every generation. Fails closed to the
    # generic fallback (never ships a mismatched render) rather than raising
    # and breaking the whole creative call.
    for url in (problem_url, solution_url):
        result = await verify_skin_rendering(url)
        if result["contains_person"] and not result["matches_target_range"]:
            print(f"[VSG01] Problem/Solution failed skin-tone check: {result['notes']}", flush=True)
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
    return {
        "items": items,
        "total_label": str(d.get("total_label", "Total")).strip() or "Total",
        "total_amount": str(d.get("total_amount", "")).strip(),
    }


async def _build_receipt(business_name: str, category: str, description: str, tokens: dict,
                         photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    from .ad_formats import receipt
    content = await _content_receipt(description)
    if not content or not content["total_amount"]:
        return None
    try:
        document = receipt.build_document(
            content["items"], content["total_label"], content["total_amount"],
            business_name=business_name, canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url,
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
                             photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    if not photo_url:
        return None
    real = await _extract_real_quote(description)
    if not real:
        return None
    try:
        document = review_card.build_document(
            photo_url, real["quote"], real["attribution"] or "A happy customer",
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


# ── SEED-082: Text on a Face (needs a real, attested customer photo) ─────

async def _content_text_on_a_face(business_name: str, category: str, description: str,
                                  correction: str = "") -> Optional[str]:
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
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, write ONE "
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
                                photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    if not photo_url:
        return None
    statement = await _content_text_on_a_face(business_name, category, description)
    if not statement:
        return None

    def _try_build(stmt: str):
        # permission_on_file=True: the attestation step upstream (the user
        # confirming this is a real customer's photo before this call ever
        # happens) IS the permission confirmation this format requires.
        return text_on_a_face.build_document(
            photo_url, stmt, permission_on_file=True, canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url,
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

async def _content_offer(business_name: str, category: str, description: str) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, write a "
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
                                   photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    if not photo_url:
        return None
    real = await _extract_real_quote(description)
    offer = await _content_offer(business_name, category, description)
    if not real or not offer:
        return None
    try:
        document = testimonial_offer.build_document(
            photo_url, real["quote"], real["attribution"] or "A happy customer",
            offer["offer_text"], permission_on_file=True,
            price_or_terms=offer["price_or_terms"], canvas_size=_CANVAS_SIZE, tokens=tokens,
            brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Testimonial + Offer build failed: {e}", flush=True)
        return None
    return document


# ── SEED-088: Starter Pack (recomposite-only — needs a clean product cutout) ─

async def _content_starter_pack(business_name: str, category: str, description: str) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, this "
        "business's product will sit in a flat-lay grid among 3-6 OTHER everyday items that "
        "belong to the same lifestyle/identity as the business's real customer (VSG-01 Starter "
        "Pack format — never build this on an ethnic, regional, or religious stereotype; the "
        "identity must be one the audience would claim willingly, e.g. 'a Lagos gym-goer's kit', "
        "not a group label).\n"
        "- product_label: a short label for the business's own product (from its name/category)\n"
        "- items: 3-6 objects, each {\"description\": \"a short scene description for generating a "
        "flat-lay photo of it, e.g. 'a pair of running shoes'\", \"label\": \"short caption, one line\"}\n"
        "Return ONLY the JSON with exactly these 2 keys."
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
    return {"product_label": product_label, "items": items}


async def _build_starter_pack(business_name: str, category: str, description: str, tokens: dict,
                              photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    if not photo_url:
        return None
    content = await _content_starter_pack(business_name, category, description)
    if not content:
        return None
    item_descriptions = [i[0] for i in content["items"]]
    item_labels = [i[1] for i in content["items"]]
    width, height = _CANVAS_SIZE
    import math
    cols = math.ceil(math.sqrt(len(item_descriptions) + 1))
    cell_size = f"{width // cols}x{width // cols}"
    try:
        item_urls = []
        for desc in item_descriptions:
            item_urls.append(await generate_scene(starter_pack._item_prompt(desc), size=cell_size))
    except SceneGenerationFailed as e:
        print(f"[VSG01] Starter Pack item generation failed: {e}", flush=True)
        return None

    try:
        document = starter_pack.build_document(
            item_urls, item_labels, photo_url, content["product_label"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
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
                                 photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
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

async def _content_text_only(business_name: str, category: str, description: str) -> Optional[dict]:
    """Same truthfulness contract as _content_receipt/_content_price_led_offer
    — this format has NO image to fall back on, so §6.14 is explicit the
    headline must carry a real fact, never invented sentiment. Only ever
    fires when the business's own words state something concrete."""
    if not (description or "").strip():
        return None
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, does the "
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
                           photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    content = await _content_text_only(business_name, category, description)
    if not content:
        return None
    from .ad_formats import text_only
    try:
        document = text_only.build_document(
            content["headline"], subline=content["subline"], action_line=content["action_line"],
            canvas_size=_CANVAS_SIZE, tokens=tokens, brand_logo_url=brand_logo_url,
        )
    except Exception as e:
        print(f"[VSG01] Text-Only build failed: {e}", flush=True)
        return None
    return document


# ── SEED-098: Work In Progress (generated scene — see module
# docstring on why this doesn't yet accept a real work photo) ─────────────

async def _content_work_in_progress(business_name: str, category: str, description: str) -> Optional[dict]:
    prompt = (
        f"For a Nigerian ad for {_business_line(business_name, category, description)}, describe "
        "the everyday hands-on WORK this business does (e.g. 'installing solar panels', 'fixing a "
        "burst pipe', 'repairing a generator') and write one short line naming what's being done "
        "and, if known, where they serve (e.g. 'Solar install underway — Lekki Phase 1').\n"
        "- trade_activity: a short, concrete VISUAL scene for a documentary photo (what a camera "
        "would see — hands, tools, the job partially complete), no brand/person names\n"
        "- statement: the short line, 6 words or fewer\n"
        f"- nigerian_setting: pick the single best-fitting option, copied EXACTLY, from this list: "
        f"{list(_NIGERIAN_SETTINGS)}\n"
        "Return ONLY the JSON with exactly these 3 keys."
    )
    d = await _call_content_model(prompt)
    if not d:
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
                                  photo_url: Optional[str] = None, brand_logo_url: Optional[str] = None):
    content = await _content_work_in_progress(business_name, category, description)
    if not content:
        return None
    from .ad_formats import work_in_progress
    try:
        scene_url = await generate_scene(
            work_in_progress._scene_prompt(content["trade_activity"], content["nigerian_setting"]),
            size=f"{_CANVAS_SIZE[0]}x{_CANVAS_SIZE[1]}",
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
}


async def render_vsg01_creative(
    strategy: Strategy, business_name: str, category: str, description: str,
    brand_context: Optional[dict] = None, photo_url: Optional[str] = None,
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

    document = await builder(
        business_name, category, description, tokens, photo_url=photo_url, brand_logo_url=brand_logo_url,
    )
    if document is None:
        return None

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


def _vsg01_candidate_params(
    *, photo_url: Optional[str], photo_attestation: Optional[str], recomposite: bool,
) -> tuple[Optional[frozenset], bool, bool, Optional[str]]:
    """The exact candidate_ids/has_product_photo/has_real_customer_photo logic
    select_and_render_vsg01_creative already used inline — factored out so the
    pre-generation suggest-format endpoint (router.py's POST /jane-ads/creative/
    suggest-format) computes the SAME eligible pool a real generation call
    would, rather than a second hand-maintained copy that could drift."""
    if photo_url and photo_attestation:
        candidate_ids = RECOMPOSITE_PHOTO_FORMAT_IDS if recomposite else UPLOAD_PHOTO_FORMAT_IDS
        has_product_photo = photo_attestation == "product_photo"
        has_real_customer_photo = photo_attestation == "real_customer_photo"
        return candidate_ids, has_product_photo, has_real_customer_photo, photo_url
    return NO_PHOTO_FORMAT_IDS, False, False, None


async def select_and_render_vsg01_creative(
    db, business_name: str, category: str, description: str,
    brand_context: Optional[dict] = None, *,
    photo_url: Optional[str] = None,
    photo_attestation: Optional[str] = None,
    recomposite: bool = False,
    forced_format_id: Optional[str] = None,
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
    see `_content_fit_boost`) > plain eligibility order. Each layer only
    reorders what the layer below it already found eligible — none of them
    can make an ineligible format render.
    """
    candidate_ids, has_product_photo, has_real_customer_photo, photo_url = _vsg01_candidate_params(
        photo_url=photo_url, photo_attestation=photo_attestation, recomposite=recomposite,
    )

    ranked = await select_ranked_ad_formats(
        db, has_product_photo=has_product_photo, has_real_customer_photo=has_real_customer_photo,
        candidate_ids=candidate_ids, description=description,
    )
    ranked_ids = {s.strategy_id for s in ranked}
    effective_forced = forced_format_id if forced_format_id in ranked_ids else None
    if effective_forced is None:
        for preferred_id in (brand_context or {}).get("ad_format_selections") or []:
            if preferred_id in ranked_ids:
                effective_forced = preferred_id
                break
    if effective_forced:
        ranked = (
            [s for s in ranked if s.strategy_id == effective_forced]
            + [s for s in ranked if s.strategy_id != effective_forced]
        )
    for strategy in ranked:
        result = await render_vsg01_creative(
            strategy, business_name, category, description, brand_context, photo_url=photo_url,
        )
        if result is not None:
            return result
    return None
