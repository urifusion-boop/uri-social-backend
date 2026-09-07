"""
VSG-01 v3 — the format-selection + generation orchestrator (§6-9, step 10).

Every format module and every supporting primitive (brand_tokens, visual_slots,
legibility, skin_tone_check, isolation_cap, attribute_tagging) was built and
shipped with the same note in its own docstring: "not yet wired into a live
call path... this is the primitive that step calls." This module is that
step — the one place that actually calls them together, in order, around a
real render.

SCOPE OF THIS FIRST WIRING. All 12 formats are real, retrievable corpus
records (step 9) and `select_ad_format` below applies the full VSG-01
eligibility logic to all of them via the SAME retrieval engine every other
corpus category already uses (retrieval.py's exclusion_reason/retrieve —
nothing format-specific was added there). But only three formats are wired to
actually GENERATE a creative from this module today:

    SEED-075 (Us vs Them), SEED-087 (Borrowed Interface), SEED-080 (Problem/Solution)

These three, and only these three, need nothing this codebase can't already
supply honestly:
  - no real photo of anything (asset_source is "drawn" or "generate" — never
    upload_as_is/recomposite),
  - no isolation-capped account (none of the three sets requires_isolation),
  - no human-in-the-loop review step (unlike Humour/Cartoon, whose own
    build_document HARD-BLOCKS on human_reviewed=True with no default — there
    is no review workflow wired to this endpoint to satisfy that honestly).

The other 8 (Receipt, Day 1->Day 30, Review Card, Testimonial+Offer,
Text on a Face, News Headline, The Censored Item, Starter Pack, plus
Humour/Cartoon) are deliberately NOT auto-rendered here, each for a reason
the format's own module already states, not a gap in this one:
  - Receipt needs real, currently-honoured prices this pipeline has no
    source for — inventing one is exactly what §2.5 forbids.
  - Day 1->Day 30 / Review Card / Testimonial+Offer's person path / Text on
    a Face / The Censored Item / Starter Pack all require a REAL photo
    (product or customer) that retrieval.py's Requirement gate already
    protects — `select_ad_format` is called from the GENERATE path with
    has_product_photo=has_real_customer_photo=False, so these correctly
    never surface here. Wiring them up is real future work, at the
    creative_from_upload/recomposite call sites, once there is a genuine
    signal for "this specific uploaded photo is a real product/customer
    photo with permission on file" — assuming any upload qualifies would be
    the misrepresentation risk §1.2 exists to prevent, not a shortcut.
  - News Headline is requires_isolation=True and `isolated_ad_account` has
    no real signal anywhere in this codebase yet (grep confirms it) — it
    correctly never survives retrieval until that signal exists.
  - Humour/Cartoon's human_reviewed gate is described above.

Falls back to the existing generic image (`generate_ad_image`) at every
possible failure point — selection returning nothing, an LLM content call
failing, a built document failing its own legibility check, a generated
scene failing its skin-tone check, or the render call itself failing. Never
raises; `select_and_render_vsg01_creative` returns None on any of these,
exactly like `generate_ad_image` already does.
"""
from __future__ import annotations

import json
from typing import Optional

from app.core.config import settings

from .ad_formats import borrowed_interface, problem_solution, us_vs_them
from .ad_formats.attribute_tagging import build_ad_format_attributes
from .ad_formats.brand_tokens import resolve_brand_tokens
from .ad_formats.legibility import check_legibility
from .entities import ConsumedBy, Strategy, StrategyCategory, StrategyPlatform
from .layer2_generation import SceneGenerationFailed, generate_scene
from .retrieval import BudgetContext, BusinessProfile, RetrievalRequest, retrieve
from .skin_tone_check import verify_skin_rendering
from .store import MongoStrategyStore
from .vsg01_corpus_seed import FORMAT_MODULES

# See module docstring — the only three formats this orchestrator will
# actually build a creative for today. `select_ad_format` is given this set
# as its candidate pool from the GENERATE path so retrieval only ever
# ranks among formats this module can honestly finish.
AUTO_RENDER_FORMAT_IDS = frozenset({"SEED-075", "SEED-087", "SEED-080"})

# The format library's own tested/documented canvas — every format module's
# unit tests and hard-check reasoning (line wrapping, scrim heights, column
# widths) assume this square shape. Ad placements elsewhere in this codebase
# are 9:16 vertical (creative.py's _AD_IMAGE_PLATFORM/_AD_IMAGE_TYPE) — moving
# these formats to a vertical canvas is real future work needing a per-format
# layout review (anchoring, empty space below shorter content), not a
# same-day resize.
_CANVAS_SIZE = (1080, 1080)


async def select_ad_format(
    db, *,
    has_product_photo: bool = False,
    has_real_customer_photo: bool = False,
    isolated_ad_account: bool = False,
    candidate_ids: Optional[frozenset] = None,
) -> Optional[Strategy]:
    """The actual §6 retrieval-time gate, applied to the CREATIVE_FORMATS
    category specifically. Reuses retrieval.py's exclusion_reason/retrieve
    unchanged — a format corpus record is excluded/scored exactly like every
    other category's record; nothing here is format-specific logic
    duplicating what retrieval.py already does.

    `candidate_ids` restricts the pool BEFORE retrieval (not a fake
    exclusion reason) — the GENERATE call site passes AUTO_RENDER_FORMAT_IDS
    so a corpus record this module can't yet finish never gets selected in
    the first place, whatever its score. Returns the top-ranked Strategy, or
    None if nothing in the pool is eligible (caller falls back to generic
    generation)."""
    if db is None:
        return None
    approved = await MongoStrategyStore(db).fetch_approved()
    candidates = [
        s for s in approved
        if s.category is StrategyCategory.CREATIVE_FORMATS
        and (candidate_ids is None or s.strategy_id in candidate_ids)
    ]
    if not candidates:
        return None

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
    result = retrieve(candidates, req)
    return result.records[0] if result.records else None


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


async def _build_us_vs_them(business_name: str, category: str, description: str, tokens: dict):
    rows = await _content_us_vs_them(business_name, category, description)
    if not rows:
        return None
    try:
        document = us_vs_them.build_document(rows, canvas_size=_CANVAS_SIZE, tokens=tokens)
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


async def _build_borrowed_interface(business_name: str, category: str, description: str, tokens: dict):
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


async def _build_problem_solution(business_name: str, category: str, description: str, tokens: dict):
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
            canvas_size=_CANVAS_SIZE, tokens=tokens,
        )
    except Exception as e:
        print(f"[VSG01] Problem/Solution build failed: {e}", flush=True)
        return None
    return document


_BUILDERS = {
    "SEED-075": _build_us_vs_them,
    "SEED-087": _build_borrowed_interface,
    "SEED-080": _build_problem_solution,
}


async def render_vsg01_creative(
    strategy: Strategy, business_name: str, category: str, description: str,
    brand_context: Optional[dict] = None,
) -> Optional[dict]:
    """Build + render the selected format. Returns
    {"png_bytes": bytes, "format_id": str, "attributes": dict} on success,
    None on any failure (caller falls back to generic generation) — never
    raises, matching every other creative.py entry point's own contract."""
    builder = _BUILDERS.get(strategy.strategy_id)
    if builder is None:
        return None
    format_def = FORMAT_MODULES[strategy.strategy_id].FORMAT
    tokens = resolve_brand_tokens((brand_context or {}).get("brand_colors"))

    document = await builder(business_name, category, description, tokens)
    if document is None:
        return None

    try:
        from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService
        png_bytes = await DocumentRendererService.render_to_png(document)
    except Exception as e:
        print(f"[VSG01] render_to_png failed for {strategy.strategy_id}: {e}", flush=True)
        return None

    attributes = build_ad_format_attributes(
        format_def, document,
        # None of the three auto-rendered formats depict a specific real
        # person: Us vs Them/Borrowed Interface are pure drawn text/shapes,
        # and Problem/Solution's generated scenes are checked for a person
        # above but never assert one belongs to a real, named customer.
        has_person=False, person_is_real=False,
    )
    return {"png_bytes": png_bytes, "format_id": strategy.strategy_id, "attributes": attributes}


async def select_and_render_vsg01_creative(
    db, business_name: str, category: str, description: str,
    brand_context: Optional[dict] = None,
) -> Optional[dict]:
    """The one call creative.py's GENERATE path needs: select a format from
    the real corpus (scoped to what this module can honestly finish today —
    see AUTO_RENDER_FORMAT_IDS and the module docstring), then build+render
    it. None at any step means "use the existing generic image path" —
    exactly the fallback creative.py already has for a failed generic
    generation, so wiring this in changes nothing about the failure mode a
    caller has to handle."""
    strategy = await select_ad_format(db, candidate_ids=AUTO_RENDER_FORMAT_IDS)
    if strategy is None:
        return None
    return await render_vsg01_creative(strategy, business_name, category, description, brand_context)
