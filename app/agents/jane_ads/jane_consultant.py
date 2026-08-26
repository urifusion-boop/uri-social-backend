"""
Jane + Ads — the strategic consultant layer (jane-strategy-extraction v1.1.0).

Replaces the old staged, checklist-style field extraction with a genuine consultant:
forms a hypothesis early, hunts for the intermediary and the trigger, reasons properly
about geography (own-radius / watering-hole / mixed / non-local), scales question depth
to the budget tier, and is willing to say a creative won't work. One LLM call per turn,
given the full accumulated brief (the same flattened brief-so-far string the router has
always sent) — returns either another question (stage="ask") or the finished, confirmed
brief (stage="ready").

The output extends ParsedCampaign (nl.py) so `to_campaign_request()` and every existing
downstream consumer (decision_engine, the wallet's backwards-budget conversion, creative,
summary) keep working completely unchanged — only the geo section in router.py reads the
new strategic fields (geo_mode/geo_areas) when the consultant has populated them.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import openai
from pydantic import Field

from app.core.config import settings

from .nl import parse_ngn
from .models import Goal, OfferType, PurchaseBehaviour
from .nl import NlUnavailableError, ParsedCampaign

_GOALS = {g.value for g in Goal}
_OFFER_TYPES = {o.value for o in OfferType}
_BEHAVIOURS = {b.value for b in PurchaseBehaviour}
_GEO_MODES = {"own_radius", "watering_hole", "mixed", "non_local"}


class ConsultantBrief(ParsedCampaign):
    """ParsedCampaign + the strategic reasoning a real consultant produces (system
    prompt sections 3 and 7). `missing`/`clarify` (inherited) still drive the existing
    need_more early-return in router.py — the consultant deliberately never populates
    `missing` with a chip-triggering value; this is a real conversation, not a form."""
    geo_mode: Optional[str] = None                       # own_radius|watering_hole|mixed|non_local
    geo_areas: list[dict] = Field(default_factory=list)  # [{"name": "...", "reason": "..."}]
    geo_explanation: str = ""
    intermediary_note: str = ""    # one sentence, only when an intermediary beats the end user
    creative_fit_warning: str = "" # §8 — set only when a creative won't serve the stated goal
    stated_plan: str = ""          # the plain-language "here's what I'll do" line (§7.6) —
                                    # required once ready, folded into the plan's explanation


SYSTEM_PROMPT = """You are Jane, a growth consultant for African small businesses — primarily \
Nigerian. You are not a form, a chatbot, or an ad tool. You are the marketing expert this \
business could never afford to hire.

Your job in this conversation is to understand the business well enough to build a campaign \
that actually gets them customers. Then to build it.

## 1. HOW AN EXPERT ACTUALLY THINKS

A mediocre consultant collects answers to a checklist. An expert does five things differently:

They form a hypothesis early and test it. After two facts about a business, an expert already \
has a working theory of where its customers come from. They spend the conversation testing \
that theory, not gathering data to build one later.

They ask about money before tactics. What is a customer worth? How many can you handle? An \
expert who doesn't know this cannot size anything correctly.

They listen for what isn't said. "I want more followers" usually means sales. "My ads didn't \
work" usually means they ran one boosted post. The stated request is a symptom; diagnose the \
cause.

They look for the binding constraint. Often the reason a business isn't growing has nothing to \
do with advertising — poor location, slow replies, no differentiation, no capacity. Name the \
real constraint even when it isn't the thing you were asked about.

They know when the answer is "not this." Tell a client when advertising is the wrong move \
right now.

Behave like that consultant, not like software collecting parameters.

## 2. WHAT YOU MUST ESTABLISH

Seven things. Infer what you can, confirm what matters, ask only what you cannot get otherwise:
(1) what is actually sold, specifically; (2) who pays and who decides (often an intermediary —
developer, planner, contractor, procurement officer, parent); (3) what triggers the need — the \
event in someone's life shortly before they buy; (4) where that trigger is visible — does it \
convert into a place you can target?; (5) the economics — what a customer is worth, how many \
the business can handle; (6) the real goal of this campaign, often not the first-stated one; \
(7) the budget — determines how deep this conversation goes.

## 3. THE EXPERT MOVES

**Find the intermediary.** Does someone buy this repeatedly on behalf of others? If yes, they \
are almost always the better target. Solar → developers, new-estate residents. Canopy/chair \
rental → event planners. Packaging → the trade clusters where producers operate. Uniforms → \
schools, not parents. Catering equipment → caterers, not households. The naive target is the \
end user — check for the intermediary before accepting it.

**Find the trigger, not the interest.** "Who wants this" is weak. "What happens the week \
before someone needs this" is strong — triggers convert into places and timing.

**Check capacity before generating demand.** If a business handles four jobs a month, don't \
build a campaign generating forty leads. Size to what they can actually handle.

**Check what happens after the lead arrives.** If nobody answers WhatsApp for six hours, no \
campaign works. Ask how fast they respond; if it's poor, say fixing that matters more than \
budget.

## 4. DEPTH SCALES WITH BUDGET

Never put a small-budget client through an interrogation.

| Tier | Budget (NGN) | Questions | Produces |
|---|---|---|---|
| 1 | below ~15,000 | 0-2 | one audience, tight geography, one creative |
| 2 | ~15,000-50,000 | 2-3 | one or two audiences, one creative direction |
| 3 | ~50,000-250,000 | 4-6 | multiple audiences, creative variants, testing |
| 4 | above ~250,000 | full conversation | segmented structure, multiple angles |

The rule for every question: would the plan change based on the answer, at THIS budget? If \
not, don't ask it.

## 5. HOW TO RUN THE CONVERSATION

Start from what you already know (given to you as context) — never ask what's already \
answered. State what you believe rather than asking open questions: "I've got you as a solar \
installer around Lekki and Ajah, mostly residential. Still right?"

Then confirm your strategic assumption — state your theory of where customers come from and \
invite correction: "Rather than chasing homeowners generally, I think your better target is \
people fitting out new places — new estates and developers. Does that match where your jobs \
come from?" If corrected, re-derive the whole strategy, don't patch one field.

When the trigger is invisible (a pipe bursts, a screen cracks) — say so plainly, and switch to \
intent-capture (be there when they search) or familiarity (be known before the need arises). \
Never degrade into "target everyone nearby."

Interrogate the stated goal once when it looks like a symptom ("more followers" → usually \
sales; "more awareness" → usually nobody is buying). Ask what a good month looks like in \
customers or money — that's the real goal.

Respect the tier's question cap. Near the cap and still uncertain: choose the assumption least \
damaging if wrong, state it clearly as an assumption, and proceed.

## 6. WHAT YOU DERIVE (never ask separately)

Platform — from purchase behaviour: do customers search for this or discover it? Geography — \
governed entirely by section 7. Structure — from the budget tier. Creative type — from who \
pays plus the goal (B2B needs credibility/proof; consumer discovery needs native, phone-shot \
content; services need a face). Angle — from the trigger.

## 7. GEOGRAPHY

The most powerful precise lever, and the one most wasted. Establish three DIFFERENT things — \
do not assume one implies the others: where the business is based; where they can actually \
serve (a HARD constraint — a caterer in Yaba may serve all of Lagos, a barber serves a few \
streets, a solar installer travels but cost caps how far); where their customers actually are \
(may be nowhere near either — the watering-hole case).

Then choose the mode:
- OWN_RADIUS — business pulls customers to its location (salon, clinic, restaurant, shop) —
  target a radius around THEM.
- WATERING_HOLE — business must go to where customers gather (solar→estates/developers,
  canopy rental→event planners, B2B→trade clusters) — target the places customers COLLECT.
- MIXED — both: a tight radius AND focused on specific pockets within it (premium daycare:
  drop-off distance + affluent estates).
- NON_LOCAL — geography barely relevant (online-only, nationwide delivery, customers outside
  the country). Say so rather than forcing a local strategy.

Sizing: everyday/low-value/frequent → very tight radius (people don't cross the city for a \
haircut). Considered/specialist → wider (people travel for a good clinic). Delivered → the \
delivery radius is the boundary, full stop. Hard catchment (schools, gyms, daycare) → beyond a \
distance, conversion is IMPOSSIBLE, not just unlikely. A small budget tightens geography — \
concentrate on a smaller area before narrowing anything else; but don't over-narrow past what \
the platform needs to optimise (roughly 1km minimum radius).

Prefer NAMED POCKETS over broad districts — "target Surulere" filters almost nothing; pin the \
specific commercial street, estate, or pocket where the customer actually concentrates. \
Exclusions matter as much as inclusions — note areas beyond the service radius or that don't \
convert.

**State the geography plan back — mandatory, never skip.** Tell the client, in plain language, \
where you'll target and why, as a sentence they can react to: "I'll focus on the newer estates \
around Ajah and Sangotedo rather than homeowners generally, since solar usually gets bought \
when people are fitting out. Do those areas match where your jobs come from?" You cannot be \
relied on for verified current local facts — the client can confirm or correct. Never assert a \
specific place name as established fact; always phrase it as a suggestion to confirm.

## 8. CREATIVE FIT

Be willing to say a creative — theirs or one you'd make — won't serve the goal. Brand-story \
video when the goal is messages this week builds familiarity, not action. Consumer-trend \
content aimed at a B2B intermediary is the wrong register. Silent product footage for a \
service business — services sell on people. No location shown when the goal is walk-ins. \
Multiple variants at Tier 1 starves them all. Name the GOAL being undermined, not the flaw \
("this won't get you messages this week," not "this is too long"). Offer the alternative, then \
respect their decision if they proceed anyway.

## 9. HARD CONSTRAINTS

Restricted categories (financial services, health/medical claims, housing, employment, credit, \
alcohol, content involving minors) carry platform restrictions — flag before building anything. \
Never use scraped/harvested data. If the budget can't support a workable campaign, say so \
plainly rather than quietly taking money for something that can't work. Label every estimate as \
an estimate, never a promise. Never assert an unverified local fact (specific street/estate/ \
business name) as established — mark it as a suggestion to confirm. If the binding constraint \
is elsewhere (no differentiation, no capacity, no response time, budget far below the floor), \
say so and recommend the real fix even when it isn't a campaign.

## REFERENCE

Meta's daily floor is about ₦1,610 (cheapest entry, good for discovery). Google has no fixed \
floor but needs real click volume (10-20/day) to work. TikTok's ad-group floor is around \
₦31,000 and needs video — only worth it for larger budgets. Budget tiers: Tier 1 below ₦15,000, \
Tier 2 ₦15,000-50,000, Tier 3 ₦50,000-250,000, Tier 4 above ₦250,000. Minimum useful targeting \
radius is roughly 1km — think tight pins on named pockets, not single streets.
"""


def _output_instructions() -> str:
    return (
        "Return ONLY a JSON object, no prose outside it, in one of two shapes.\n\n"
        "If — per your own tiered-depth judgment — you still need to ask something before you "
        "can build this campaign, return:\n"
        '{"stage": "ask", "clarify": "<your next message to the client, in your own voice — '
        'may state an assumption AND ask a question, per section 5>"}\n\n'
        "Once you have enough to build the plan (the business, offer_type, a budget or a stated "
        "customer-count goal, and your own geography judgment per section 7), return:\n"
        "{\n"
        '  "stage": "ready",\n'
        '  "business_name": "...", "category": "...",\n'
        f'  "goal": one of {sorted(_GOALS)},\n'
        f'  "offer_type": one of {sorted(_OFFER_TYPES)},\n'
        '  "budget_ngn": number or null, "desired_conversions": integer or null,\n'
        '  "city": "...",\n'
        f'  "stated_behaviour": one of {sorted(_BEHAVIOURS)} or null,\n'
        '  "is_new_thing": bool, "has_existing_demand": bool, "has_video": bool,\n'
        '  "geo_mode": "own_radius"|"watering_hole"|"mixed"|"non_local",\n'
        '  "geo_areas": [{"name": "...", "reason": "..."}],\n'
        '  "geo_explanation": "one sentence — why these areas/this mode",\n'
        '  "intermediary_note": "one sentence if an intermediary beats the end-user target, else '
        'empty string",\n'
        '  "creative_fit_warning": "one sentence if a supplied/planned creative won\'t serve the '
        'goal, else empty string",\n'
        '  "stated_plan": "REQUIRED — the plain-language sentence stating your geography/audience '
        'choice back to the client for confirmation (section 7.6). Never skip this."\n'
        "}\n"
        "Do NOT invent budget_ngn/desired_conversions — leave null if genuinely not stated. Do "
        "NOT assert a specific place name as verified fact; phrase geo_areas/stated_plan as "
        "suggestions the client can correct.\n\n"
        "NON-NEGOTIABLE before you say \"ready\": you must have a REAL budget_ngn or "
        "desired_conversions THE CLIENT STATED FOR THIS CAMPAIGN (a remembered past campaign's "
        "spend does not count on its own — you may offer it as a suggestion, but never silently "
        "assume it applies here), AND a real geo_mode with either geo_areas or an explicit "
        "non_local reason. These two are the ones a real media buyer NEVER guesses or skips, "
        "however deep into the conversation you are — if either is still missing, you MUST return "
        "stage=\"ask\" and request exactly the missing one, no matter how many questions you've "
        "already asked.\n\n"
        "Exception that resolves this WITHOUT asking again: if you explicitly ASK the client "
        "whether to reuse a remembered figure (\"want to do the same ₦5,000 again?\") and they "
        "reply with a plain affirmative (\"yes\", \"sure\", \"that works\", \"same\", etc.), that "
        "IS them stating it for THIS campaign — set budget_ngn to that figure and move on. Do NOT "
        "ask the identical budget question again just because they answered with a word instead "
        "of retyping the number; that reads as broken and repetitive to the client.\n\n"
        "BUT — once budget_ngn (or desired_conversions) AND geography are BOTH genuinely "
        "established, that is enough to build a first plan. Do not keep asking for MORE precision "
        "on top of that:\n"
        "- desired_conversions is a NICE-TO-HAVE, never a second requirement once budget_ngn is "
        "real. If the client says something vague like \"as much as possible\" or \"as many as I "
        "can get\", that means \"no specific target\" — leave desired_conversions null and move "
        "on; do NOT ask again for an exact number.\n"
        "- offer_type: if the category/business context already tells you what's being sold "
        "(e.g. a social media marketing agency is obviously offering a SERVICE), infer it "
        "yourself rather than asking again — only ask if it's genuinely ambiguous.\n"
        "- Never ask two turns in a row for close variants of the same thing (e.g. \"what "
        "service do you offer\" then \"can you specify the offer\") — if you already asked and "
        "got any answer at all, however imperfect, use your best judgment and move forward."
    )


_NO_BUSINESS_CLARIFY = (
    "What would you like to promote? Tell me a bit about your business or what you're selling."
)


def build_history_turns(saved: list[dict]) -> list[dict]:
    """Turn a thread's saved chat messages into real OpenAI turns (role/content pairs),
    oldest first. Without this, the consultant only ever sees the frontend's flattened
    "brief so far" — a bag of the CLIENT's own fragments with no idea which answer went
    with which question ("the product. 100. individuals. lekki. yes. yes.") — and can
    never converge, re-asking overlapping questions forever. Jane's own prior questions
    (kind="text" or a "result" carrying `question`) become assistant turns so the model
    can actually track what it already asked and what was answered."""
    turns: list[dict] = []
    for m in saved:
        role = m.get("role")
        kind = m.get("kind")
        if kind == "text":
            text = (m.get("text") or "").strip()
            if text:
                turns.append({"role": "user" if role == "user" else "assistant", "content": text})
        elif kind == "result" and role == "jane":
            result = m.get("result") or {}
            if result.get("question"):
                turns.append({"role": "assistant", "content": result["question"]})
            elif result.get("stage") in ("planned", "launched"):
                expl = ((result.get("plan") or {}).get("explanation") or "").strip()
                if expl:
                    turns.append({"role": "assistant", "content": f"[I presented a plan: {expl[:500]}]"})
    return turns[-24:]   # cap context size — recent turns matter most


async def consult(message: str, business_name: str = "", category: str = "",
                  known_budget: Optional[float] = None,
                  history: Optional[list[dict]] = None,
                  offering: str = "") -> ConsultantBrief:
    """One consultant turn. `history` — the real prior turns of THIS conversation
    (see build_history_turns) — is what lets the consultant actually track state
    across turns instead of re-deriving confusion from a jumbled flat string each
    time. Raises NlUnavailableError if the model call fails — same contract as
    nl.py's parse_message, so an outage never masquerades as a 'need more info'
    follow-up (the caller surfaces it as a clear 503 instead)."""
    if not settings.jane_ads_openai_key:
        raise NlUnavailableError("OPENAI_API_KEY is not configured")
    if not (message or "").strip():
        if not business_name and not category:
            return ConsultantBrief(missing=["business_name"], clarify=_NO_BUSINESS_CLARIFY)
        return ConsultantBrief(business_name=business_name, category=category)

    known_bits = []
    if business_name:
        known_bits.append(f"business name: {business_name}")
    if category:
        known_bits.append(f"category: {category}")
    if offering:
        # What they actually sell, from the brand profile. Without it the consultant
        # has only a name and an industry label, and plans come out generic — a
        # category is not an offer, and the offer is what an audience responds to.
        known_bits.append(f"what they sell: {offering}")
    if known_budget:
        known_bits.append(f"last campaign they spent ₦{known_budget:,.0f} (a PAST campaign — "
                          "do not treat this as THIS campaign's budget; you may offer it as a "
                          "suggestion, but only a budget the client states for THIS campaign counts)")
    known_line = (f"Already known about this client — {', '.join(known_bits)}."
                  if known_bits else "Nothing known about this client yet.")

    # A light nudge against genuinely unbounded looping — only once the conversation is
    # ACTUALLY long (real questions asked, not a remembered past budget or an incidental
    # digit). budget_ngn/desired_conversions and geography are still HARD requirements
    # (enforced below in code, not just prompted) regardless of this nudge, because an
    # earlier, more aggressive version of this nudge caused Jane to skip budget and area
    # entirely and fabricate a plan — never repeat that failure mode.
    questions_asked = sum(1 for t in (history or []) if t.get("role") == "assistant")
    cap_nudge = ""
    if questions_asked >= 5:
        cap_nudge = (
            f"\n\nYou have asked {questions_asked} questions already — that's a lot for any "
            "tier. If you genuinely still lack the budget or the area/geography, ask for "
            "EXACTLY that (nothing else) one more time. Otherwise converge now."
        )

    budget_confirmation_note = _build_budget_confirmation_note(known_budget, message, history or [])

    # `message` is the frontend's flattened "brief so far" (every user reply in this
    # conversation, concatenated) — redundant with `history` above when history is
    # present, but the only signal at all on the very first turn. Framed as a summary,
    # not "their latest message", so the model doesn't mistake it for one new answer.
    prompt = (
        f"{known_line}\n\n"
        f'Everything the client has told you so far, summarized: "{message}"'
        f"{cap_nudge}{budget_confirmation_note}\n\n{_output_instructions()}"
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        # This is a much harder job than the old mechanical field extraction (nl.py) —
        # genuine multi-constraint synthesis and hypothesis tracking across turns.
        # gpt-4o-mini was not reliably converging (re-asking near-identical questions
        # even once budget and area were both already established); gpt-4o handles the
        # long, nuanced system prompt far more coherently.
        resp = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *(history or []),
                {"role": "user", "content": prompt},
            ],
            timeout=35,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[Consultant] error: {e}", flush=True)
        raise NlUnavailableError(str(e)) from e

    brief = _coerce(data, business_name, category)
    return _enforce_hard_requirements(brief, message, history or [], known_budget)


_AFFIRMATIVE_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct", "right",
    "same", "fine", "good", "confirmed", "please", "go",
}


def _latest_user_reply(message: str) -> str:
    """The client's newest reply out of `message` (the frontend's flattened, ". "-joined
    brief-so-far) — everything after the last period. No length cap, unlike
    `_client_gave_affirmative`'s tail check, since a substantive answer ("10000, ikeja")
    can run longer than a bare "yes" while still being exactly one fresh reply."""
    return (message or "").strip().split(".")[-1].strip().lower()


def _client_gave_affirmative(message: str) -> bool:
    """True if the client's most recent reply reads like a short yes/no-style
    confirmation ('yes', 'that's fine') rather than a fresh, substantive answer —
    the shape of reply Jane gets when she's just asked 'want to do the same again?'."""
    tail = _latest_user_reply(message)
    if not tail or len(tail) > 40:
        return False
    words = {w.strip(",!?'\"") for w in tail.split()}
    return bool(words & _AFFIRMATIVE_WORDS)


# Money the client names for THIS campaign. Requires a naira marker (₦/N/NGN), a
# k/m suffix, or a bare figure of at least 4 digits — so "100 customers" and "7 days"
# are not mistaken for a budget, while "20k", "₦20,000" and "20000" are.
_NUM = r"\d[\d,]*(?:\.\d+)?"
_STATED_AMOUNT = re.compile(
    rf"(?:₦|\bngn\b|\bn(?=\d))\s*({_NUM})\s*([km])?"   # ₦20,000 / N20k / NGN 50000
    rf"|\b({_NUM})\s*([km])\b"                            # 20k / 5k / 1.5m
    r"|\b(\d{4,})\b",                                     # bare 20000
    re.I,
)


def stated_budget_ngn(reply: str) -> Optional[float]:
    """The budget the client just named, if they named exactly one.

    Two or more distinct figures in one reply is genuinely ambiguous ("20k for ads,
    5k for design"), so this returns None and lets Jane ask rather than guessing
    which one was meant.
    """
    from .nl import parse_ngn

    found: list[float] = []
    for m in _STATED_AMOUNT.finditer(reply or ""):
        digits = m.group(1) or m.group(3) or m.group(5)
        suffix = m.group(2) or m.group(4) or ""
        if not digits:
            continue
        v = parse_ngn(f"{digits}{suffix}")
        if v and v >= 1000:
            found.append(v)
    uniq = sorted(set(found))
    return uniq[0] if len(uniq) == 1 else None


def _build_budget_confirmation_note(known_budget: Optional[float], message: str,
                                    history: list[dict]) -> str:
    """Pre-resolve the one ambiguity prompting alone couldn't reliably get the model to
    commit to (confirmed live: it kept re-asking the identical budget question even after
    being told a plain "yes" counts, and separately, confirmed live 2026-08-12, even after
    the client retyped the exact figure themselves — "10000, ikeja" — since the `known_line`
    warning against silently carrying the remembered figure forward made the model treat
    that restatement as ambiguous too). Covers both shapes of a genuine decision: a bare
    affirmative directly answering Jane's own proposal to reuse the figure, or the client
    restating the number outright — tell the model plainly what just happened either way,
    instead of leaving it to infer from a prompt it's already shown it can misread."""
    latest = _latest_user_reply(message)
    stated_now = stated_budget_ngn(latest)

    # No remembered budget at all — a fresh thread. The function used to bail here,
    # so a plainly stated figure got no note and the model was left to decide for
    # itself whether to accept it. It is a coin flip: the identical message converged
    # in one turn over the API and took two in the UI, asking "can you confirm this
    # budget is still accurate" about a number the client had just typed. Say it
    # plainly instead of leaving it to chance.
    if not known_budget:
        if stated_now:
            return (
                f"\n\nIMPORTANT: the client's latest reply above states ₦{stated_now:,.0f} as "
                f"THIS campaign's budget. It is stated, not implied — do not ask them to "
                f"confirm it. Set budget_ngn to {int(stated_now)} and move on to whatever's "
                "next (not budget again)."
            )
        return ""

    if _client_gave_affirmative(message):
        last_assistant = next((t.get("content", "") for t in reversed(history)
                               if t.get("role") == "assistant"), "")
        if str(int(known_budget)) in last_assistant.replace(",", ""):
            return (
                f"\n\nIMPORTANT: your last message asked the client whether to reuse their "
                f"remembered budget of ₦{known_budget:,.0f}, and their reply above is a plain "
                f"affirmative — they ARE confirming ₦{known_budget:,.0f} for THIS campaign. "
                f"Set budget_ngn to {int(known_budget)} and move on to whatever's next (not "
                "budget again)."
            )
    latest_reply = latest

    # The client named a DIFFERENT figure to the remembered one. This was the gap:
    # the two branches below only fire when the reply repeats the remembered amount,
    # so stating a new budget matched nothing, the known_line kept advertising the old
    # spend, and Jane re-asked — offering the past figure back. Live-confirmed on
    # 2026-08-26: remembered ₦10,000, client said "budget 20000", Jane asked again.
    stated = stated_now
    if stated and stated != known_budget:
        return (
            f"\n\nIMPORTANT: the client's latest reply above states ₦{stated:,.0f} for THIS "
            f"campaign. That REPLACES the remembered ₦{known_budget:,.0f} — do not offer the "
            f"old figure back. Set budget_ngn to {int(stated)} and move on to whatever's next "
            "(not budget again)."
        )

    if str(int(known_budget)) in latest_reply.replace(",", ""):
        return (
            f"\n\nIMPORTANT: the client's latest reply above states ₦{known_budget:,.0f} "
            "themselves — that is a deliberate answer for THIS campaign, not a silent "
            f"carryover of the remembered past spend. Set budget_ngn to {int(known_budget)} "
            "and move on to whatever's next (not budget again)."
        )
    return ""


def _budget_grounded(amount: Optional[float], known_budget: Optional[float],
                     message: str, history: list[dict]) -> bool:
    """True if `amount` is actually something the CLIENT said in this conversation, not
    the model silently carrying forward a remembered past campaign's spend. A budget
    that differs from the remembered figure is presumably new and trusted outright; one
    that matches it exactly only counts if the client's OWN words contain that number
    (not just Jane's paraphrase of it) — OR the client gave a plain "yes" directly in
    reply to JANE HERSELF proposing that figure ("want to do the same again?"). That's
    still a genuine, deliberate answer for THIS campaign, not a silent carry-over —
    without this, a client who answers "yes" instead of retyping the number gets asked
    the identical budget question again and again (confirmed live)."""
    if amount is None or amount <= 0:
        return False
    if known_budget is None or abs(amount - known_budget) > 1:
        return True
    needle = str(int(amount))
    client_texts = [message] + [t.get("content", "") for t in history if t.get("role") == "user"]
    if any(needle in txt for txt in client_texts):
        return True
    last_assistant = next((t.get("content", "") for t in reversed(history) if t.get("role") == "assistant"), "")
    # Naira amounts in Jane's own text are comma-formatted ("₦5,000") — strip commas
    # so the digit match isn't defeated by formatting alone.
    return needle in last_assistant.replace(",", "") and _client_gave_affirmative(message)


def _enforce_hard_requirements(brief: ConsultantBrief, message: str, history: list[dict],
                               known_budget: Optional[float]) -> ConsultantBrief:
    """A real media buyer never guesses or skips the budget and the area — enforced HERE,
    not just prompted, because prompting alone already let two failure modes through
    once: treating a remembered past-campaign spend as this campaign's confirmed budget,
    and skipping geography/area entirely. If the model's "ready" claim doesn't actually
    satisfy both, downgrade it back to "ask" for exactly the missing one."""
    if brief.missing or brief.clarify:
        return brief   # already an "ask" — nothing to enforce

    if not (brief.desired_conversions or _budget_grounded(brief.budget_ngn, known_budget, message, history)):
        return ConsultantBrief(
            business_name=brief.business_name, category=brief.category,
            goal=brief.goal, offer_type=brief.offer_type,
            clarify="What budget would you like to spend on this specific campaign?",
        )
    if not brief.geo_mode and not brief.city:
        return ConsultantBrief(
            business_name=brief.business_name, category=brief.category,
            goal=brief.goal, offer_type=brief.offer_type,
            budget_ngn=brief.budget_ngn, desired_conversions=brief.desired_conversions,
            clarify="Which area or city should I focus this campaign on — or is location not really relevant for this business?",
        )
    return brief


def _coerce(data: dict, business_name: str, category: str) -> ConsultantBrief:
    """Normalize raw LLM JSON into a validated ConsultantBrief (defensive about types
    and stage, mirroring nl.py's own _coerce discipline)."""
    def _num(v):
        # Shared with nl.py — clients type "20k", and float() turning that into
        # None makes Jane re-ask for a budget she was just given.
        return parse_ngn(v) if v not in (None, "", "null") else None

    def _int(v):
        n = _num(v)
        return int(n) if n and n > 0 else None

    stage = str(data.get("stage") or "ask").lower()
    resolved_business = str(data.get("business_name") or business_name or "").strip()
    resolved_category = str(data.get("category") or category or "").strip()

    if stage != "ready":
        clarify = str(data.get("clarify") or _NO_BUSINESS_CLARIFY).strip()
        return ConsultantBrief(
            business_name=resolved_business,
            category=resolved_category,
            # Never a chip-triggering value — this is a real conversation, not a form.
            missing=[] if (resolved_business or resolved_category) else ["business_name"],
            clarify=clarify,
        )

    goal = str(data.get("goal") or "").lower().replace("-", "_") or None
    offer_type = str(data.get("offer_type") or "").lower().replace("-", "_") or None
    beh = str(data.get("stated_behaviour") or "").lower() or None
    geo_mode = str(data.get("geo_mode") or "").lower() or None
    geo_areas = [a for a in (data.get("geo_areas") or []) if isinstance(a, dict) and a.get("name")]

    return ConsultantBrief(
        business_name=resolved_business,
        category=resolved_category,
        goal=goal if goal in _GOALS else None,
        offer_type=offer_type if offer_type in _OFFER_TYPES else None,
        budget_ngn=_num(data.get("budget_ngn")),
        desired_conversions=_int(data.get("desired_conversions")),
        city=str(data.get("city") or "").strip(),
        stated_behaviour=beh if beh in _BEHAVIOURS else None,
        is_new_thing=bool(data.get("is_new_thing")),
        has_existing_demand=bool(data.get("has_existing_demand")),
        has_video=bool(data.get("has_video")),
        geo_mode=geo_mode if geo_mode in _GEO_MODES else None,
        geo_areas=geo_areas,
        geo_explanation=str(data.get("geo_explanation") or "").strip(),
        intermediary_note=str(data.get("intermediary_note") or "").strip(),
        creative_fit_warning=str(data.get("creative_fit_warning") or "").strip(),
        stated_plan=str(data.get("stated_plan") or "").strip(),
    )
