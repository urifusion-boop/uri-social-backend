"""
VSG-01 v3 corpus seed (§6, step 9) — the 12 ad formats as real corpus
records.

§6: "Jane retrieves the format from the corpus, not from this document.
This library governs how a selected format renders." VSG-01's own header
already frames these as corpus records: "Source: Ad Format Templates
category, corpus records SEED-074 to SEED-095." This module is that
category, populated: one Strategy record per format module in
app/agents/jane_ads/ad_formats/, category=CREATIVE_FORMATS.

**Built as Strategy objects directly, not via corpus.py's row_to_strategy/
import_rows.** That pipeline exists for the human-edited Records
spreadsheet (ASC-SPEC-01 v2 §4) and is real, tested, production
infrastructure for other categories — deliberately not touched here.
It also has a real, separate gap: row_to_strategy never reads a
"Requires" column into Strategy.requires at all (checked directly — no
such mapping exists in corpus.py), so it couldn't carry
Requirement.PRODUCT_PHOTO/REAL_CUSTOMER_PHOTO even if asked to. That gap
doesn't need fixing here: these 12 records aren't spreadsheet-authored
data needing the workbook-label translation layer corpus.py exists for —
they're system-derived directly from each format module's own
AdFormatDef (already exact, already typed), so building Strategy objects
directly is the more honest fit, not a workaround.

**Evidence grading is deliberately conservative, not fabricated
confidence.** evidence_grade=C (not A/B): "the identity must be one the
audience claims willingly" for a real B/A grade is live outcome
confirmation, and none exists yet for these formats — that's exactly what
§4's attribute-tagging → campaign_outcome join is for. C, not D: D has
weight 0.0 (entities.py: "D is excluded upstream and must never be
scored") and retrieval's own confidence threshold (0.40) means a D-graded
record can never actually retrieve regardless of anything else — grading
these D would import them into permanent unreachability, defeating the
entire point of this step. C is documented, reasoned design (this
document's own citations of prior corpus records and named mechanisms)
without live confirmation — the honest middle, not the safe-looking
bottom.

market_origin=NIGERIA_DESK_RESEARCH, not NIGERIA: entities.py's own
distinction is exact — "`nigeria` means evidence observed in a Nigerian
account. `nigeria_desk_research` means reasoning about Nigeria from
published sources." No live Nigerian-account outcome exists yet for any
of these; claiming NIGERIA would be a false claim of evidence this module
doesn't have, and NIGERIA is the only origin that earns retrieval's 1.2x
score bonus — a bonus these records haven't earned yet either.

transfer_verdict=APPLIES_WITH_MODIFICATION for all 12: every one of these
is a globally common ad-format pattern (testimonial ads, before/after,
flat-lay collections, news-style creative exist everywhere) that VSG-01
specifically adapts for Nigeria — representation defaults (§1.7), Zone
A/B (§1.3), naira pricing, Nigerian settings. modification_required names
that adaptation per format, which the Strategy model requires whenever
this verdict is used (entities.py's own validator).

pooled_account_safe: PooledAccountSafety.REQUIRES_ISOLATION for the three
formats whose own AdFormatDef.requires_isolation is True (News Headline,
Day 1 -> Day 30, The Censored Item) — this is the actual retrieval-time
precondition (retrieval.py's exclusion_reason already checks it), the
usage-count ceiling in isolation_cap.py is the additional layer §6 says
these three "additionally require" on top of it. Every other format gets
PooledAccountSafety.YES.

requires: pulled directly from each format's own AdFormatDef.requires
(already real Requirement values as strings from Step 1) — not
re-invented here.

budget_floor_ngn_daily=0 for all 12: a creative format template has no
inherent minimum spend threshold the way a tactic like parallel-adset
testing does ("Zero is a real floor, not a missing one... an organic
tactic costs ₦0/day" — entities.py's own reasoning, applied here to "any
budget can use this format," not to an organic/paid distinction).

Ingests as DRAFT, same as every other corpus record — corpus.py's own
_guard_ingest (via store.ingest) refuses anything arriving pre-approved,
and this module doesn't try to bypass that. Real human approval
(store.approve(...)) is a separate, deliberate step; see this module's
own test suite for a full seed -> approve -> retrieve loop verified
against the real InMemoryStrategyStore and retrieval.retrieve(), not
assumed to work.
"""
from typing import List

from .ad_formats import (
    ai_answer,
    billboard_statement,
    enrol_signup,
    event_promo,
    floating_cards,
    offer_spotlight,
    portrait_headline,
    problems_vs_solution,
    borrowed_interface,
    censored_item,
    day1_day30,
    humour_cartoon,
    news_headline,
    price_led_offer,
    problem_solution,
    receipt,
    review_card,
    starter_pack,
    testimonial_offer,
    text_on_a_face,
    text_only,
    us_vs_them,
    work_in_progress,
)
from .backfill import derive_consumed_by
from .entities import (
    EvidenceGrade,
    MarketOrigin,
    PooledAccountSafety,
    Requirement,
    SalesCycle,
    Strategy,
    StrategyCategory,
    StrategyPlatform,
    StrategyStatus,
    TransferVerdict,
)
from .store import StrategyStore

_REQUIREMENT_BY_VALUE = {r.value: r for r in Requirement}

_RECORDS = [
    dict(
        format_module=receipt,
        claim="Use for an offer with separable components where price transparency "
              "is an advantage — it answers 'how much?' before it is asked.",
        mechanism="Itemised, right-aligned pricing composited entirely in Layer 4 "
                  "reads as the seller's own transparent quotation, not a payment "
                  "confirmation — the strongest local fit in the library.",
        business_types=["Retail / itemised goods and services"],
        modification_required="Naira formatting; must never resemble a bank transfer "
                               "alert or payment confirmation (§2.5).",
        funnel_stages=["conversion"],
    ),
    dict(
        format_module=us_vs_them,
        claim="Use when displacing an established habit or method the audience "
              "already uses.",
        mechanism="A direct two-column comparison against a generic method (never a "
                  "named competitor) makes the value of switching legible in one glance.",
        business_types=["Any business competing against an informal or manual status quo"],
        modification_required="Left column must name a method, never an identified "
                               "business (§2.4, SEED-050); Nigerian representation defaults.",
        funnel_stages=["consideration"],
    ),
    dict(
        format_module=borrowed_interface,
        claim="Use when the ad should read as a message rather than an advertisement.",
        mechanism="Evoking a familiar WhatsApp-style chat interface borrows the "
                  "credibility of a personal conversation.",
        business_types=["Owner-operated or service businesses used to fielding "
                        "WhatsApp enquiries"],
        modification_required="Android/WhatsApp styling only, never iOS/iMessage — "
                               "the audience is overwhelmingly Android (§2.6).",
        funnel_stages=["consideration"],
    ),
    dict(
        format_module=day1_day30,
        claim="Use when the change is physical and verifiable — never for health, "
              "weight, skin or appearance.",
        mechanism="A real, unmanipulated before/after pairing proves a tangible "
                  "transformation for installation, construction, repair or "
                  "training-cohort work.",
        business_types=["Construction, repair/restoration, fit-out, training providers"],
        modification_required="Excluded outright for health/weight/skin/appearance "
                               "categories (§2.9); usage capped across the book (SEED-079).",
        funnel_stages=["conversion"],
    ),
    dict(
        format_module=review_card,
        claim="Use as the default, cheapest proof asset when the business has a "
              "genuine written review.",
        mechanism="A real product photo plus a verbatim quote, and a star rating "
                  "only where one genuinely exists, is the strongest low-cost local "
                  "proof signal.",
        business_types=["Any business with at least one genuine customer review"],
        modification_required="Star block only against a real rating on a real "
                               "platform — a fabricated rating is a fraud signal "
                               "locally (§2.1).",
        funnel_stages=["conversion"],
    ),
    dict(
        format_module=problem_solution,
        claim="Default choice when nothing more specific fits — lowest policy risk "
              "in the library.",
        mechanism="Stating the problem as a naira cost and the solution as an "
                  "outcome, not a feature, is legible in the ~1-second window a "
                  "feed scroll allows.",
        business_types=["Any business, especially where no more specific format applies"],
        modification_required="Nigerian setting/lighting/skin-tone defaults on the "
                               "generated situational illustration (§1.7).",
        funnel_stages=["awareness"],
    ),
    dict(
        format_module=testimonial_offer,
        claim="Use when proof and a live offer must land in one impression, "
              "typically at low budget where a sequence is unaffordable.",
        mechanism="Belief — a real customer's proof — must be established before "
                  "the offer is read, or the offer alone reads as pressure.",
        business_types=["Low-budget advertisers who cannot afford a multi-step funnel"],
        modification_required="The person must be real with permission on file, or "
                               "no person at all (§2.2, §1.2).",
        funnel_stages=["conversion"],
    ),
    dict(
        format_module=text_on_a_face,
        claim="Use for an owner-operated or service business with a real person to "
              "feature.",
        mechanism="Proximity and accountability — a specific human standing behind "
                  "the business — is the credibility mechanism; a generated face "
                  "defeats it entirely.",
        business_types=["Owner-operated or service businesses (repairs, trades, "
                        "consulting)"],
        modification_required="Highest policy risk in the library — never a "
                               "presumption about the viewer, never touches health/"
                               "body/finances/personal circumstance, owner or "
                               "consenting real customer only (§2.7).",
        funnel_stages=["consideration"],
    ),
    dict(
        format_module=news_headline,
        claim="Use only where there is a genuine announcement — strongest on "
              "institutional and education accounts.",
        mechanism="Stating the actual news, never a 'Breaking News' label, earns "
                  "attention in a form readers are already trained to scan.",
        business_types=["Institutional and education accounts with real announcements"],
        modification_required="Never a sensational banner-tag label, never imitates "
                               "a specific broadcaster (§2.8); usage capped across "
                               "the book (SEED-079).",
        funnel_stages=["awareness"],
    ),
    dict(
        format_module=censored_item,
        claim="Use only where there is a real, genuine pending reveal.",
        mechanism="A hard-edged redaction bar over the real product creates "
                  "anticipation without misrepresenting what will actually be "
                  "revealed.",
        business_types=["Businesses with a genuine unreleased product, design, or "
                        "dated launch"],
        modification_required="The obscured item must be the real product, never "
                               "generated; never obscures price (§2.10); usage capped "
                               "across the book (SEED-079).",
        funnel_stages=["awareness"],
    ),
    dict(
        format_module=starter_pack,
        claim="Use when the audience is defined by a recognisable identity or "
              "situation the audience claims willingly.",
        mechanism="Placing the client's real product among items that visually "
                  "belong to a shared identity signals in-group relevance without "
                  "saying so.",
        business_types=["Businesses selling into a specific lifestyle or identity "
                        "segment"],
        modification_required="Never built on ethnic, regional or religious "
                               "stereotype; the client's product must be real, "
                               "never generated alongside it (§2.11).",
        funnel_stages=["awareness"],
    ),
    dict(
        format_module=humour_cartoon,
        claim="Use when the brand can carry it and the joke is about a shared "
              "situation, not a group.",
        mechanism="A single-panel visual punchline about a shared situation is "
                  "memorable and lowers guard without needing a caption to land.",
        business_types=["Brands with a voice that can carry humour"],
        modification_required="Needs human review before shipping on the ₦15k tier "
                               "— the only format in the library where that applies "
                               "(§2.12).",
        funnel_stages=["awareness"],
    ),
    # VSG-01-PROMPTS v2 §6.13-6.15 — the 3 formats the v2 doc added on top of
    # v3's original 12. Built the same way as the 12 above: read straight off
    # each module's own AdFormatDef, not spreadsheet-authored.
    dict(
        format_module=price_led_offer,
        claim="Use for a straightforward priced offer — probably the most common "
              "Nigerian SME ad pattern, and the one the original 12 formats missed.",
        mechanism="A product photo with the price as the largest element after the "
                  "product, plus delivery area and payment method, answers 'how much "
                  "and how do I get it' in one glance.",
        business_types=["Any business selling a single priced product or package"],
        modification_required="Price must be current and honoured; no struck-through "
                               "'was' price unless genuinely charged; delivery area and "
                               "payment methods user-confirmed before entering live "
                               "creative.",
        funnel_stages=["conversion"],
    ),
    dict(
        format_module=text_only,
        claim="Use when the business has no usable photograph at all, or the message "
              "is purely informational.",
        mechanism="One line of real information — a price, a delivery area, a "
                  "specific offer, a real fact — set large on a plain field stays "
                  "legible even under heavy compression, with nothing else in frame "
                  "to fail.",
        business_types=["Any business with no product photography available"],
        modification_required="The line must carry real information, not sentiment — "
                               "abstraction fails harder here than anywhere else in "
                               "the library; minimum 72px, 7:1 contrast, compression-"
                               "tested.",
        funnel_stages=["awareness", "conversion"],
    ),
    dict(
        format_module=work_in_progress,
        claim="Use for a service business with no product to photograph — "
              "installers, trades, clinics, schools.",
        mechanism="Work visibly underway — hands, tools, a partially completed job — "
                  "proves capability the way a product photo proves it for a business "
                  "that sells a physical item.",
        business_types=["Service businesses with no product to photograph "
                        "(installation, repair, trades, clinics, schools)"],
        modification_required="Prefer the client's own photographs over generated "
                               "ones — a generated installation is not their work; no "
                               "implied completed-job claim on generated imagery; no "
                               "safety-violating depiction (no unprotected work at "
                               "height, no exposed live electrical work).",
        funnel_stages=["consideration"],
    ),
    dict(
        format_module=event_promo,
        claim="Use for a dated event — a webinar, class, workshop, challenge, masterclass or launch.",
        mechanism="A poster reads at a glance: a huge title, what kind of event it is, the date on its "
                  "own pill and one action — the viewer knows what, when and how to join in one look, "
                  "and a real host's face makes it a person they're showing up for.",
        business_types=["Coaches, trainers, schools, consultants, communities and any business "
                        "running a dated event"],
        modification_required="The date, host and any price must be the business's own; a host is "
                               "only shown from the owner's own photo, never generated; say 'free' "
                               "only when the event is free.",
        funnel_stages=["conversion", "consideration"],
    ),
    dict(
        format_module=offer_spotlight,
        claim="Use for a real discount, price cut or deal — especially a service business with several offerings.",
        mechanism="A warm everyday photo stops the scroll, the services sit as bright tags, and the "
                  "offer appears twice — on a tilted ticket and in colour inside the headline — so it "
                  "can't be missed.",
        business_types=["Service businesses, agencies, salons, studios, shops running a promotion"],
        modification_required="The offer and every figure must be the business's own and currently "
                               "honoured; the photo shows a situation, never a fake customer or the "
                               "product.",
        funnel_stages=["conversion"],
    ),
    dict(
        format_module=problems_vs_solution,
        claim="Use when the customer's everyday frustrations are the clearest way in.",
        mechanism="The viewer recognises their own complaints in the chat bubbles on the left, and "
                  "the right side answers them — the real product, or what changes for them — so "
                  "the business reads as the obvious fix.",
        business_types=["Any business solving a recognisable everyday frustration — skincare, "
                        "software, cleaning, logistics, food"],
        modification_required="Frustrations in the customer's plain words, outcomes stated as "
                               "results, no invented figures; the product is only ever the real, "
                               "photographed product.",
        funnel_stages=["awareness", "consideration"],
    ),
    dict(
        format_module=portrait_headline,
        claim="Use when a warm, human face says what the business is about — farms, food, beauty, "
              "care, any business whose promise is personal.",
        mechanism="A smiling person fills the frame and the big headline runs across them, so the "
                  "promise reads as a person's, not a company's; the two-tone pill makes the next "
                  "step (where to buy) impossible to miss.",
        business_types=["Food, farms, beauty, wellness, fashion, care and personal services"],
        modification_required="The person is generated for the situation or is the owner's own "
                               "photo, never a customer's; headline and description only from the "
                               "business's own words; no invented figures.",
        funnel_stages=["awareness", "consideration"],
    ),
    dict(
        format_module=floating_cards,
        claim="Use for an app, software or service whose value shows on a screen — tracking, "
              "bookings, reminders, records, dashboards.",
        mechanism="Someone at ease with the product, with pieces of its screen floating beside "
                  "them, shows the result before anyone reads a word; the headline names the worry "
                  "the product removes.",
        business_types=["Apps, software, SaaS, fintech, booking and management tools"],
        modification_required="Cards show what the product does in the business's own words, with no "
                               "figure it didn't give; the person is generated, never a named customer.",
        funnel_stages=["awareness", "consideration"],
    ),
    dict(
        format_module=enrol_signup,
        claim="Use for enrolment, admissions, registration, a course intake or a membership sign-up.",
        mechanism="A happy learner or member makes the future feel near; the big capitals headline "
                  "and the 'secure your seat' button make it one step, and the real phone number and "
                  "email remove the last doubt.",
        business_types=["Schools, colleges, training centres, academies, bootcamps, gyms and clubs"],
        modification_required="Contact details are only the business's own; no invented dates, "
                               "deadlines, prices or places left; the person is generated.",
        funnel_stages=["conversion", "consideration"],
    ),
    dict(
        format_module=billboard_statement,
        claim="Use for one bold, confident statement the business stands for.",
        mechanism="A real-looking roadside billboard gives a single line public weight; the "
                  "highlighted turn of the phrase is what people remember.",
        business_types=["Any business with a clear point of view — agencies, consultants, "
                        "coaches, brands"],
        modification_required="The statement is the business's own view, with no figures, "
                               "guarantees or claims about competitors; every word is set by our "
                               "renderer, never painted by the image model.",
        funnel_stages=["awareness"],
    ),
    dict(
        format_module=ai_answer,
        claim="Use when customers have a real question the business answers — what to do next, how "
              "to choose, where to start.",
        mechanism="People already ask AI assistants these questions; seeing their own question "
                  "answered with useful advice, then the business that does it, feels like a tip, "
                  "not an ad.",
        business_types=["Services people research first — education, travel, health, finance, "
                        "consulting"],
        modification_required="A generic chat look with no real AI product's name, logo or styling, "
                               "so no third-party endorsement is implied; the answer only states "
                               "what the business offers.",
        funnel_stages=["awareness", "consideration"],
    ),
]


# The reverse of the table above — strategy_id -> the format module that
# renders it. Step 9's own corpus records are the single source of truth for
# which module a given SEED-0xx id maps to; the orchestrator (step 10) looks
# a retrieved Strategy's format up here rather than hand-maintaining a second
# list that could drift from this one.
FORMAT_MODULES = {record["format_module"].FORMAT.format_id: record["format_module"] for record in _RECORDS}


def _requires_for(format_def) -> List[Requirement]:
    return [_REQUIREMENT_BY_VALUE[value] for value in format_def.requires]


def _pooled_account_safe_for(format_def) -> PooledAccountSafety:
    return (
        PooledAccountSafety.REQUIRES_ISOLATION
        if format_def.requires_isolation
        else PooledAccountSafety.YES
    )


def build_vsg01_strategies() -> List[Strategy]:
    """The 12 VSG-01 format records, as real Strategy objects — draft
    status, ready to ingest. Never pre-approved (see module docstring)."""
    strategies = []
    for record in _RECORDS:
        format_def = record["format_module"].FORMAT
        strategies.append(Strategy(
            strategy_id=format_def.format_id,
            version=1,
            status=StrategyStatus.DRAFT,
            category=StrategyCategory.CREATIVE_FORMATS,
            claim=record["claim"],
            mechanism=record["mechanism"],
            evidence_grade=EvidenceGrade.C,
            market_origin=MarketOrigin.NIGERIA_DESK_RESEARCH,
            transfer_verdict=TransferVerdict.APPLIES_WITH_MODIFICATION,
            modification_required=record["modification_required"],
            business_types=record["business_types"],
            budget_floor_ngn_daily=0.0,
            platforms=[StrategyPlatform.META],
            funnel_stages=record.get("funnel_stages", []),
            sales_cycle=SalesCycle.NOT_APPLICABLE,
            consumed_by=derive_consumed_by(StrategyCategory.CREATIVE_FORMATS),
            pooled_account_safe=_pooled_account_safe_for(format_def),
            requires=_requires_for(format_def),
            ingested_by="vsg01_import",
        ))
    return strategies



# VSG-01-PROMPTS v2 §6.13-6.15 all now have real modules (price_led_offer,
# text_only, work_in_progress — see _RECORDS above) and no longer belong here.
# Kept as an empty list, not removed outright, so router.py's own "any entry
# still in here is undocumented-in-code" contract (GET /jane-ads/ad-formats'
# "planned" status) stays meaningful if a future v3 doc adds more formats
# before their modules exist.
PLANNED_FORMAT_RECORDS = []


async def seed_vsg01_corpus(store: StrategyStore) -> int:
    """Ingest all 12 VSG-01 format records into `store` as draft — real
    human approval (store.approve(...)) is a separate, deliberate step
    this function does not perform. Returns the count ingested."""
    strategies = build_vsg01_strategies()
    for strategy in strategies:
        await store.ingest(strategy)
    return len(strategies)
