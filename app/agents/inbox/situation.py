"""
Unified Inbox — the situational personality engine (Personality PRD §9).

Each turn gets one situation (an angry complaint, a hurried buyer, a confused
first-timer…) and a temporary overlay on the brand's voice for that turn only. The
overlay is computed from a copy: the stored profile is never touched, so a playful
brand is still playful for the next customer after this one was angry.

The labels are signals, not facts. A business rule (an exclusion, a handoff) always
outranks them, and nothing here can make a reply send that would otherwise wait.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

EMOTIONS = ("calm", "excited", "confused", "frustrated", "angry", "abusive")

SITUATIONS = {
    "safety": "Safety-sensitive",
    "abuse": "Abusive message",
    "complaint": "Complaint",
    "refund": "Refund",
    "delivery_issue": "Delivery issue",
    "urgency": "In a hurry",
    "confusion": "Confused",
    "price_objection": "Price objection",
    "buying_intent": "Ready to buy",
    "excited": "Excited",
    "returning": "Returning customer",
    "first_enquiry": "First enquiry",
    "uncertain_fact": "Needs a fact we don't have",
    "general": "General",
}

# After this long without a message, a customer writing again is "returning".
RETURNING_AFTER = timedelta(days=7)

_HURRY = r"\b(asap|urgent(ly)?|right now|quick(ly)?|hurry|immediately|today please|fast)\b"
_WANTS_HUMAN = (r"\b(speak|talk|chat)\s+(to|with)\s+(a\s+)?(human|person|someone|somebody|agent|"
                r"customer (care|service)|manager|real person)\b|\breal person\b|"
                r"\bhuman (agent|being)\b|\bcall me\b|\bcustomer care\b")
_ASKS_IF_AI = (r"\b(are|r) (you|u) (a |an )?(bot|robot|ai|human|real|person|machine)\b|"
               r"\bis this (a )?(bot|robot|ai|automated|human)\b|\bam i (talking|chatting) (to|with) (a )?(bot|human|person)\b")
_PRICE_OBJECTION = (r"too (expensive|much|costly|high)|so expensive|\breduce\b|\bdiscount\b|"
                    r"last price|cheaper|\bcost(s)? too\b|can'?t afford|\bpricey\b")
_DELIVERY = (r"\b(deliver(y|ed)?|order|package|parcel|shipment|dispatch|rider|waybill|"
             r"tracking|arrive[d]?|haven'?t (got|received)|not (come|arrived|received))\b")
_CONFUSED = r"\b(confus(ed|ing)|don'?t understand|not clear|what do you mean|how does (it|this) work)\b|\?\?+"
_ABUSE = r"\b(idiot|stupid|fool|useless|nonsense|rubbish|scam(mer)?s?|thie(f|ves)|mumu|ode|fuck|shit)\b"


def _hit(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, (text or "").lower()))


def signals(data: dict, text: str) -> dict:
    """The situation half of a classification: the model's view, backed by words."""
    emotion = data.get("emotion") if data.get("emotion") in EMOTIONS else "calm"
    if _hit(_ABUSE, text):
        emotion = "abusive"
    return {
        "emotion": emotion,
        "hurry": bool(data.get("hurry")) or _hit(_HURRY, text),
        # Asking for a person is honoured whatever the model thought.
        "wants_human": bool(data.get("wants_human")) or _hit(_WANTS_HUMAN, text),
        "asks_if_ai": _hit(_ASKS_IF_AI, text),
    }


def _aware(at: Any) -> Optional[datetime]:
    if not isinstance(at, datetime):
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def stage_of(earlier: list[dict], at: Optional[datetime] = None) -> str:
    if not earlier:
        return "first_contact"
    last = _aware(earlier[-1].get("received_at"))
    if last and at and at - last > RETURNING_AFTER:
        return "returning"
    return "ongoing"


def situation_of(cls: dict, text: str, stage: str) -> str:
    """One situation per turn, the most serious first: a refund request that is also
    excited is still a refund request."""
    exclusions = cls.get("exclusions") or []
    emotion = cls.get("emotion", "calm")
    if "safety" in exclusions:
        return "safety"
    if emotion == "abusive":
        return "abuse"
    if cls.get("intent") == "complaint" or emotion in ("angry", "frustrated"):
        return "delivery_issue" if _hit(_DELIVERY, text) and cls.get("intent") != "praise" \
            else "complaint"
    if "refund" in exclusions:
        return "refund"
    if cls.get("intent") == "support" and _hit(_DELIVERY, text):
        return "delivery_issue"
    if cls.get("hurry") or cls.get("urgency") == "high":
        return "urgency"
    if emotion == "confused" or _hit(_CONFUSED, text):
        return "confusion"
    if _hit(_PRICE_OBJECTION, text):
        return "price_objection"
    if cls.get("intent") in ("purchase", "booking"):
        return "buying_intent"
    if emotion == "excited" or (cls.get("sentiment") == "positive" and cls.get("intent") == "praise"):
        return "excited"
    if stage == "returning":
        return "returning"
    if stage == "first_contact":
        return "first_enquiry"
    return "general"


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


# Each overlay: traits forced to a ceiling (max) or floor (min), and the rule of
# operation for that situation (§9 table). Applied to a copy only.
_OVERLAYS: dict[str, dict[str, Any]] = {
    "safety": {"max": {"humour": 0, "enthusiasm": 20, "sales_assertiveness": 0, "emoji": 0,
                       "proactivity": 20},
               "min": {"empathy": 75, "formality": 55},
               "rules": ["Be serious and careful. Do not give safety, medical or legal advice; "
                         "say a team member will follow up."]},
    "abuse": {"max": {"humour": 0, "enthusiasm": 10, "sales_assertiveness": 0, "emoji": 0,
                      "warmth": 45},
              "min": {"formality": 55},
              "rules": ["Stay neutral and firm. Do not match their tone, argue or apologise for "
                        "things that didn't happen. Offer to help with the actual issue."]},
    "complaint": {"max": {"humour": 0, "enthusiasm": 25, "sales_assertiveness": 0, "emoji": 0},
                  "min": {"empathy": 80},
                  "rules": ["Acknowledge the problem first, then say what happens next. "
                            "Never joke, never upsell, never blame the customer."]},
    "refund": {"max": {"humour": 0, "sales_assertiveness": 0, "emoji": 5},
               "min": {"formality": 55, "directness": 60},
               "rules": ["Be professional and clear. Do not promise or refuse a refund unless "
                         "a source states the policy."]},
    "delivery_issue": {"max": {"humour": 0, "sales_assertiveness": 0, "emoji": 0},
                       "min": {"empathy": 70, "directness": 65},
                       "rules": ["Be accountable and specific. Never invent a delivery status or "
                                 "date; ask for the order number if it isn't in the conversation."]},
    "urgency": {"max": {"verbosity": 20, "humour": 10, "question_frequency": 25},
                "min": {"directness": 75},
                "rules": ["They are in a hurry: lead with the answer, keep it to the essentials."]},
    "confusion": {"max": {"verbosity": 55, "humour": 15},
                  "min": {"empathy": 60},
                  "rules": ["Be patient and simple. Break it into short steps, or ask one "
                            "question about what's unclear."]},
    "price_objection": {"max": {"sales_assertiveness": 45, "humour": 20},
                        "min": {"empathy": 55},
                        "rules": ["Be respectful and consultative. Use only value points or "
                                  "alternatives from the sources; never invent a discount."]},
    "buying_intent": {"max": {"verbosity": 40},
                      "min": {"directness": 65},
                      "rules": ["Be direct and helpful: give the next step to buy, using only "
                                "the purchase route in the sources."]},
    "excited": {"min": {"enthusiasm": 55, "warmth": 65},
                "rules": ["Match their energy moderately and move to a useful next step."]},
    "returning": {"min": {"warmth": 60},
                  "rules": ["Welcome them back naturally; don't re-introduce the business."]},
    "first_enquiry": {"min": {"warmth": 55},
                      "rules": ["Answer promptly and warmly. No long introduction."]},
    "uncertain_fact": {"max": {"sales_assertiveness": 20},
                       "rules": ["Be transparent that this needs checking. Don't guess."]},
    "general": {},
}


def overlay(traits: dict[str, int], situation: str,
            policy: Optional[dict] = None) -> dict[str, Any]:
    """The voice for this one turn. `policy` is the brand's own situation answers
    (§4 G and D), which refine the default rule rather than replace the guardrail."""
    spec = _OVERLAYS.get(situation, {})
    adjusted = dict(traits)
    for k, cap in (spec.get("max") or {}).items():
        if k in adjusted:
            adjusted[k] = min(adjusted[k], cap)
    for k, floor in (spec.get("min") or {}).items():
        if k in adjusted:
            adjusted[k] = max(adjusted[k], floor)
    rules = list(spec.get("rules") or [])
    policy = policy or {}

    if situation in ("complaint", "delivery_issue"):
        first = policy.get("anger_first") or []
        order = [p for p in ("acknowledge", "apologize", "investigate", "next_steps") if p in first]
        if order:
            words = {"acknowledge": "acknowledge how they feel", "apologize": "apologise if "
                     "something went wrong on our side", "investigate": "say you'll look into it",
                     "next_steps": "give the next step"}
            rules.append("In this order: " + ", then ".join(words[o] for o in order) + ".")
        tone = policy.get("serious_complaint")
        if tone == "calm_direct":
            adjusted["verbosity"] = min(adjusted.get("verbosity", 50), 35)
            adjusted["directness"] = max(adjusted.get("directness", 50), 75)
        elif tone == "empathetic":
            adjusted["formality"] = max(adjusted.get("formality", 50), 55)
    if situation == "confusion" and policy.get("confusion"):
        how = {"simplify": "simplify", "steps": "use short numbered steps",
               "ask": "ask what part is unclear", "example": "give a quick example"}
        picks = [how[c] for c in policy["confusion"] if c in how]
        if picks:
            rules.append("When confused, " + " and ".join(picks) + ".")
    if situation == "urgency" and policy.get("hurry") == "answer_step":
        rules.append("Give the answer plus the single next step.")
    elif situation == "urgency" and policy.get("hurry") == "choices":
        rules.append("Offer at most two short choices.")
    if situation == "price_objection" and policy.get("objection"):
        how = {"value": "explain the value", "alternatives": "offer a cheaper alternative from the sources",
               "budget": "ask their budget", "acknowledge": "acknowledge it without pushing",
               "policy": "follow the pricing policy in the sources"}
        if policy["objection"] in how:
            rules.append(f"On price objections, {how[policy['objection']]}.")
    if situation == "abuse" and policy.get("abuse") == "boundary":
        rules.append("Politely say you're happy to help once the conversation stays respectful.")

    # A complaint flips humour off whatever the brand chose; so does anything the
    # brand said humour is never OK for.
    never = set(policy.get("humour_never") or [])
    if (situation in ("complaint", "delivery_issue") and {"complaints", "delays"} & never) \
            or (situation == "refund" and "payments" in never) \
            or (situation == "safety" and "health" in never):
        adjusted["humour"] = 0

    adjusted = {k: _clamp(v) for k, v in adjusted.items()}
    return {"situation": situation, "label": SITUATIONS.get(situation, situation),
            "traits": adjusted, "rules": rules}


def handoff_for(situation: str, cls: dict, policy: Optional[dict] = None) -> str:
    """A reason this turn must go to a person because of the brand's own policy, or ''."""
    policy = policy or {}
    if cls.get("wants_human"):
        return "Customer asked to speak to a person"
    if situation == "abuse" and policy.get("abuse") in ("escalate", "close"):
        return "Abusive message: your Brand Voice says a person handles these"
    if situation in ("complaint", "delivery_issue") and "escalate" in (policy.get("anger_first") or []):
        return "Angry customer: your Brand Voice says to escalate these"
    if cls.get("intent") == "complaint" and "complaints" in (policy.get("regulated_escalate") or []):
        return "Complaint: your Brand Voice sends these to a person"
    return ""
