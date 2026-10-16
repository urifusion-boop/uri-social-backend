"""
Unified Inbox — the fixed catalogue behind Brand Voice (Personality PRD §3, §4, §5, §8).

Everything here is data: the eight starting templates, the twelve traits every
profile is scored on, the 42 assessment questions, the follow-ups some answers
unlock, and the six calibration scenarios. Nothing in this file reads a customer
message, so nothing here can be steered by one.
"""
from __future__ import annotations

from typing import Any

# 0-100 each. A template is a starting point on these, never a fixed prompt.
TRAITS = ("formality", "warmth", "enthusiasm", "humour", "empathy", "directness",
          "verbosity", "proactivity", "sales_assertiveness", "emoji", "question_frequency",
          "localization")

TRAIT_LABELS = {
    "formality": ("Casual", "Formal"),
    "warmth": ("Reserved", "Warm"),
    "enthusiasm": ("Calm", "Energetic"),
    "humour": ("Serious", "Playful"),
    "empathy": ("Matter-of-fact", "Empathetic"),
    "directness": ("Gentle", "Direct"),
    "verbosity": ("Brief", "Detailed"),
    "proactivity": ("Only when asked", "Proactive"),
    "sales_assertiveness": ("Soft", "Assertive"),
    "emoji": ("No emoji", "Lots of emoji"),
    "question_frequency": ("Few questions", "Many questions"),
    "localization": ("Neutral", "Strongly local"),
}

# ── Templates (§3) ───────────────────────────────────────────────────────────

def _t(formality, warmth, enthusiasm, humour, empathy, directness, verbosity, proactivity,
       sales, emoji, questions, local) -> dict[str, int]:
    return dict(zip(TRAITS, (formality, warmth, enthusiasm, humour, empathy, directness,
                             verbosity, proactivity, sales, emoji, questions, local)))


TEMPLATES: list[dict[str, Any]] = [
    {"id": "warm_professional", "name": "Warm Professional",
     "voice": "Friendly, respectful, reassuring.",
     "suitable_for": "Education, professional services, hospitality",
     "example": "Hi! I'd be happy to help. Which service are you interested in?",
     "traits": _t(60, 75, 45, 15, 70, 55, 40, 50, 35, 10, 45, 20)},
    {"id": "friendly_nigerian", "name": "Friendly Nigerian",
     "voice": "Warm, culturally familiar, uncomplicated.",
     "suitable_for": "Local SMEs and retailers",
     "example": "Hello! Yes, we have that available. Are you looking for the smaller or bigger size?",
     "traits": _t(35, 85, 55, 30, 65, 60, 30, 55, 45, 25, 45, 70),
     "language": {"default": "en-NG"}},
    {"id": "luxury_concierge", "name": "Luxury Concierge",
     "voice": "Refined, composed, attentive.",
     "suitable_for": "Premium beauty, fashion, hotels",
     "example": "Certainly. Would you prefer something subtle or more distinctive?",
     "traits": _t(80, 60, 30, 5, 65, 50, 40, 45, 25, 0, 40, 10)},
    {"id": "energetic_salesperson", "name": "Energetic Salesperson",
     "voice": "Engaging and proactive, not pushy.",
     "suitable_for": "Fashion, ecommerce",
     "example": "Yes! It comes in black and cream. Which colour are you considering?",
     "traits": _t(30, 75, 85, 35, 55, 65, 30, 80, 65, 35, 55, 40)},
    {"id": "calm_problem_solver", "name": "Calm Problem Solver",
     "voice": "Patient, concise, solution-oriented.",
     "suitable_for": "Logistics and service teams",
     "example": "Let's check what happened. Could you share your order number?",
     "traits": _t(55, 60, 25, 5, 75, 70, 30, 50, 15, 0, 50, 20)},
    {"id": "playful_creative", "name": "Playful Creative",
     "voice": "Expressive and youthful, contextual humour.",
     "suitable_for": "Creative and youth brands",
     "example": "That one's popular for a reason 😄 Is it for everyday wear?",
     "traits": _t(20, 80, 75, 70, 55, 50, 35, 60, 40, 60, 50, 45)},
    {"id": "trusted_adviser", "name": "Trusted Adviser",
     "voice": "Knowledgeable and consultative.",
     "suitable_for": "Property, B2B, education",
     "example": "If you share your budget and preferred location, I can narrow down the options.",
     "traits": _t(65, 60, 35, 10, 60, 60, 55, 65, 40, 5, 60, 25)},
    {"id": "minimalist_assistant", "name": "Minimalist Assistant",
     "voice": "Direct and efficient.",
     "suitable_for": "Technical and operational teams",
     "example": "It's available for ₦45,000. Would you like delivery options?",
     "traits": _t(50, 45, 20, 0, 40, 90, 15, 35, 25, 0, 30, 10)},
]

TEMPLATE_BY_ID = {t["id"]: t for t in TEMPLATES}

# Q3's archetypes land on the template that already sounds like them.
ARCHETYPE_TEMPLATE = {
    "trusted_professional": "warm_professional",
    "knowledgeable_friend": "friendly_nigerian",
    "premium_adviser": "luxury_concierge",
    "enthusiastic_salesperson": "energetic_salesperson",
    "calm_problem_solver": "calm_problem_solver",
    "creative_companion": "playful_creative",
    "other": "warm_professional",
}

# ── Assessment (§4) ──────────────────────────────────────────────────────────

def _o(*pairs: str) -> list[dict[str, str]]:
    return [{"value": p.split("|")[0], "label": p.split("|")[1]} for p in pairs]


def _q(qid: str, section: str, text: str, kind: str, options: list | None = None,
       required: bool = False, max_select: int = 0, low: str = "", high: str = "",
       placeholder: str = "", prompts: list[str] | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {"id": qid, "section": section, "text": text, "type": kind,
                         "required": required}
    if options:
        q["options"] = options
    if max_select:
        q["max_select"] = max_select
    if kind == "slider":
        q["low"], q["high"] = low, high
    if placeholder:
        q["placeholder"] = placeholder
    if prompts:
        q["prompts"] = prompts
    return q


SECTIONS = [
    {"id": "A", "title": "Brand identity"},
    {"id": "B", "title": "Natural communication"},
    {"id": "C", "title": "Language and culture"},
    {"id": "D", "title": "Emotional intelligence"},
    {"id": "E", "title": "Sales behaviour"},
    {"id": "F", "title": "Customer relationships"},
    {"id": "G", "title": "Situation handling"},
    {"id": "H", "title": "Examples and calibration"},
]

ADJECTIVES = _o("friendly|Friendly", "confident|Confident", "sophisticated|Sophisticated",
                "playful|Playful", "calm|Calm", "knowledgeable|Knowledgeable", "caring|Caring",
                "energetic|Energetic", "professional|Professional", "bold|Bold",
                "approachable|Approachable", "reserved|Reserved")

QUESTIONS: list[dict[str, Any]] = [
    # A. Brand identity
    _q("q1", "A", "If your business were a person, which five adjectives best describe them?",
       "multi", ADJECTIVES, max_select=5),
    _q("q2", "A", "What three feelings should customers leave with?", "multi", _o(
        "understood|Understood", "excited|Excited", "reassured|Reassured", "respected|Respected",
        "comfortable|Comfortable", "confident|Confident", "valued|Valued", "inspired|Inspired",
        "efficiently_served|Efficiently served"), max_select=3),
    _q("q3", "A", "Which archetype best fits your brand?", "single", _o(
        "trusted_professional|Trusted professional", "knowledgeable_friend|Knowledgeable friend",
        "premium_adviser|Premium adviser", "enthusiastic_salesperson|Enthusiastic salesperson",
        "calm_problem_solver|Calm problem solver", "creative_companion|Creative companion",
        "other|Other"), required=True),
    _q("q4", "A", "Which three words should customers use to describe your brand?", "text",
       placeholder="e.g. reliable, fast, honest"),
    _q("q5", "A", "Which three words must never describe your brand?", "text",
       placeholder="e.g. pushy, cold, confusing"),
    _q("q6", "A", "How formal should your agent be?", "slider", required=True,
       low="Casual", high="Formal"),
    # B. Natural communication
    _q("q7", "B", "How long should a typical answer be?", "single", _o(
        "one_two|1–2 sentences", "brief|Brief explanation", "moderate|Moderate",
        "thorough|Thorough", "situational|Depends on the situation"), required=True),
    _q("q8", "B", "How should the agent usually open?", "single", _o(
        "formal|Formal greeting (Good day)", "friendly|Friendly greeting (Hi there!)",
        "informal|Informal greeting (Hey!)", "time_based|Time-based (Good morning)",
        "direct|Straight to the answer", "custom|Something else")),
    _q("q9", "B", "How often should it use emoji?", "single", _o(
        "never|Never", "rarely|Rarely", "occasionally|Occasionally", "frequently|Frequently",
        "customer|Only if the customer does"), required=True),
    _q("q10", "B", "How much humour?", "slider", low="Serious", high="Playful"),
    _q("q11", "B", "Preferred sentence style?", "single", _o(
        "short|Short and direct", "conversational|Conversational", "polished|Polished",
        "structured|Detailed and structured", "flexible|Flexible")),
    _q("q12", "B", "What should a reply usually do first?", "single", _o(
        "answer|Answer directly", "rapport|Build rapport", "explain|Explain carefully",
        "ask|Ask a question first", "adapt|Adapt to the customer")),
    # C. Language and culture
    _q("q13", "C", "Which languages may the agent use?", "multi", _o(
        "en|Standard English", "en_ng|Nigerian English", "pidgin|Pidgin",
        "customer_led|Switch to follow the customer"), required=True),
    _q("q14", "C", "How much local expression?", "single", _o(
        "none|None", "occasional|Occasional", "moderate|Moderate", "strong|Strong")),
    _q("q15", "C", "When a customer writes in Pidgin, the agent should…", "single", _o(
        "switch|Reply in Pidgin", "light|Use a little Pidgin", "english|Stay in English",
        "ask|Ask which they prefer")),
    _q("q16", "C", "Slang?", "single", _o(
        "never|Never", "common|Common words only", "customer_led|Only if the customer uses it",
        "frequent|Often, within brand rules")),
    _q("q17", "C", "Words, phrases or greetings you like the agent to use", "text",
       placeholder="One per line, e.g. You're welcome!"),
    _q("q18", "C", "Words or phrases the agent must never use", "text",
       placeholder="One per line, e.g. Dear valued customer"),
    _q("q19", "C", "Should the agent mirror how formal the customer is?", "single", _o(
        "yes|Yes, within limits", "slightly|Slightly", "no|No, keep our style")),
    _q("q20", "C", "How should customers be addressed?", "single", _o(
        "first_name|First name if known", "titles|Titles (Sir/Ma) when appropriate",
        "none|Avoid honorifics", "follow|Follow the customer's preference")),
    # D. Emotional intelligence
    _q("q21", "D", "When a customer is angry, what comes first?", "multi", _o(
        "acknowledge|Acknowledge how they feel", "apologize|Apologise if appropriate",
        "investigate|Investigate", "next_steps|Give next steps", "escalate|Escalate to a person")),
    _q("q22", "D", "How emotionally expressive?", "slider", low="Reserved", high="Expressive"),
    _q("q23", "D", "When a customer is confused…", "multi", _o(
        "simplify|Simplify", "steps|Break it into steps", "ask|Ask what's unclear",
        "example|Give an example")),
    _q("q24", "D", "When a customer compliments you…", "single", _o(
        "simple|Simple thanks", "warm|Warm gratitude", "enthusiastic|Enthusiastic",
        "polite|Polite acknowledgement")),
    _q("q25", "D", "How often should it acknowledge feelings?", "single", _o(
        "necessary|Only when necessary", "brief|Briefly, when relevant", "often|Often, naturally",
        "sensitive|More in sensitive cases")),
    # E. Sales behaviour
    _q("q26", "E", "How proactive with recommendations?", "slider",
       low="Only when asked", high="Proactive"),
    _q("q27", "E", "When a customer says \"too expensive\"…", "single", _o(
        "value|Explain the value", "alternatives|Offer alternatives", "budget|Ask their budget",
        "acknowledge|Acknowledge and leave it", "policy|Follow our pricing policy")),
    _q("q28", "E", "Upselling and cross-selling?", "single", _o(
        "never|Never", "relevant|Only when highly relevant", "occasionally|Occasionally",
        "actively|Actively, if it helps")),
    _q("q29", "E", "How should it close?", "single", _o(
        "soft|Soft invitation", "recommendation|A recommendation", "direct_cta|A direct call to action",
        "consultative|Consultative questions", "customer_led|Let the customer lead")),
    _q("q30", "E", "After a customer declines…", "single", _o(
        "stop|Stop", "one_alternative|Offer one alternative", "opt_in|Offer updates if they want",
        "sequence|Follow our approved sequence")),
    _q("q31", "E", "Main sales objective?", "single", _o(
        "convert|Convert quickly", "trust|Build trust", "guide|Guide the decision",
        "premium|Premium experience", "balance|A balance")),
    # F. Customer relationships
    _q("q32", "F", "Small talk?", "single", _o(
        "none|None", "little|A little", "occasional|Occasional",
        "customer|Only if the customer starts it", "frequent|Frequent if appropriate")),
    _q("q33", "F", "Refer to earlier conversations?", "single", _o(
        "yes|Yes, if useful", "session|This conversation only", "orders|Orders and support only",
        "no|No")),
    _q("q34", "F", "Returning customers?", "single", _o(
        "ordinary|Ordinary greeting", "recognise|Recognise them",
        "history|Mention relevant past conversations")),
    _q("q35", "F", "Follow-up questions?", "single", _o(
        "required|Only when required", "one|One at a time", "as_needed|As needed",
        "proactive|Proactively, if useful")),
    _q("q36", "F", "When a customer goes silent…", "single", _o(
        "none|Do nothing", "one|One reminder", "sequence|Our approved sequence",
        "handoff|Hand high-value cases to a person")),
    # G. Situation handling
    _q("q37", "G", "Customer in a hurry?", "single", _o(
        "shortest|Shortest useful answer", "answer_step|Answer plus the next step",
        "choices|Concise choices", "customer_led|Let them lead")),
    _q("q38", "G", "Abusive customer?", "single", _o(
        "calm|Stay calm", "boundary|Remind them of boundaries", "escalate|Escalate to a person",
        "close|Close under policy")),
    _q("q39", "G", "When the agent doesn't know the answer…", "single", _o(
        "verify|Say it will be verified", "search|Check approved knowledge first",
        "clarify|Ask the customer to clarify", "handoff|Hand to a person")),
    _q("q40", "G", "Serious complaint: how should the tone change?", "single", _o(
        "empathetic|More empathetic and professional", "calm_direct|Calmer and more direct",
        "minor|Only slightly", "custom|Our own approach")),
    # H. Examples and calibration
    _q("q41", "H", "How would you reply to these?", "examples", prompts=[
        "How much is this?", "Your price is too expensive.",
        "I've waited for my order and nobody is replying."]),
    _q("q42", "H", "Paste a customer exchange that sounds exactly like your brand", "text",
       placeholder="Customer: …\nUs: …"),
]

QUESTION_BY_ID = {q["id"]: q for q in QUESTIONS}

# ── Adaptive follow-ups (§5) ─────────────────────────────────────────────────

FOLLOW_UPS: dict[str, dict[str, Any]] = {
    "fu_premium_restraint": _q("fu_premium_restraint", "A",
                               "How restrained should the language be?", "slider",
                               low="Relaxed", high="Very restrained"),
    "fu_premium_discounts": _q("fu_premium_discounts", "E", "Can the agent mention discounts?",
                               "single", _o("never|Never", "asked|Only if asked",
                                            "allowed|Yes, when an FAQ lists one")),
    "fu_premium_exclusivity": _q("fu_premium_exclusivity", "A",
                                 "How do you talk about exclusivity?", "text",
                                 placeholder="e.g. limited pieces, by appointment"),
    "fu_humour_style": _q("fu_humour_style", "B", "Which humour fits you?", "multi", _o(
        "wordplay|Light wordplay", "emoji|Playful emoji", "pop|Pop culture",
        "self|Self-deprecating")),
    "fu_humour_never": _q("fu_humour_never", "G", "When is humour never OK?", "multi", _o(
        "complaints|Complaints", "payments|Payments", "delays|Delays", "health|Health or safety")),
    "fu_slang_examples": _q("fu_slang_examples", "C", "Slang you're happy with", "text",
                            placeholder="One per line"),
    "fu_pidgin_degree": _q("fu_pidgin_degree", "C", "How much Pidgin?", "single", _o(
        "light|A little, mostly English", "moderate|Moderate", "full|Full Pidgin")),
    "fu_pidgin_examples": _q("fu_pidgin_examples", "C", "Show us how you write in Pidgin",
                             "text", placeholder="e.g. Abeg, which size you want?"),
    "fu_pidgin_review": _q("fu_pidgin_review", "C",
                           "Pidgin replies are less tested than English. Should they send automatically?",
                           "single", _o("review|Hold Pidgin replies for a person to check",
                                        "auto|Send them automatically")),
    "fu_sales_boundaries": _q("fu_sales_boundaries", "E",
                              "When should the agent stop selling?", "text",
                              placeholder="e.g. after one no, never on a complaint"),
    "fu_sales_followups": _q("fu_sales_followups", "E", "Most nudges after a no?", "single",
                             _o("0|None", "1|One", "2|Two")),
    "fu_sales_never": _q("fu_sales_never", "E", "Pressure the agent must never use", "multi", _o(
        "false_urgency|False urgency (\"only 1 left!\")", "guilt|Guilt",
        "repeat|Asking again and again", "price_anchor|Inflated was-prices")),
    "fu_regulated_rules": _q("fu_regulated_rules", "G",
                             "What must the agent never advise on?", "text",
                             placeholder="e.g. diagnoses, dosages, investment returns"),
    "fu_regulated_escalate": _q("fu_regulated_escalate", "G", "Always hand these to a person",
                                "multi", _o("records|Records or account details",
                                            "advice|Requests for advice",
                                            "complaints|Complaints", "eligibility|Eligibility")),
}

REGULATED_INDUSTRIES = ("health", "medic", "clinic", "pharma", "hospital", "finance", "bank",
                        "loan", "insur", "invest", "fintech", "microfinance")


def _has(answers: dict, qid: str, value: str) -> bool:
    a = answers.get(qid)
    return a == value or (isinstance(a, list) and value in a)


def _num(answers: dict, qid: str, default: int = 50) -> int:
    try:
        return max(0, min(100, int(answers.get(qid, default))))
    except (TypeError, ValueError):
        return default


def follow_ups_for(answers: dict, industry: str = "") -> list[dict[str, Any]]:
    """The extra questions these answers unlock, in the order they should be asked."""
    ids: list[str] = []
    if _has(answers, "q3", "premium_adviser") or _has(answers, "q1", "sophisticated") \
            or _has(answers, "q31", "premium"):
        ids += ["fu_premium_restraint", "fu_premium_discounts", "fu_premium_exclusivity"]
    if _num(answers, "q10", 0) >= 65 or _has(answers, "q3", "creative_companion") \
            or _has(answers, "q1", "playful"):
        ids += ["fu_humour_style", "fu_humour_never", "fu_slang_examples"]
    if _has(answers, "q13", "pidgin") or answers.get("q15") in ("switch", "light"):
        ids += ["fu_pidgin_degree", "fu_pidgin_examples", "fu_pidgin_review"]
    if _num(answers, "q26", 0) >= 70 or _has(answers, "q28", "actively") \
            or _has(answers, "q29", "direct_cta") or _has(answers, "q31", "convert"):
        ids += ["fu_sales_boundaries", "fu_sales_followups", "fu_sales_never"]
    if any(w in (industry or "").lower() for w in REGULATED_INDUSTRIES):
        ids += ["fu_regulated_rules", "fu_regulated_escalate"]
    return [FOLLOW_UPS[i] for i in ids]


# Answers that pull in opposite directions. Averaging them gives a voice nobody
# chose, so the owner picks (§4: "do not derive a personality by averaging").
def conflicts_in(answers: dict) -> list[dict[str, Any]]:
    out = []

    def add(cid: str, text: str, *options: str) -> None:
        if f"conflict:{cid}" not in answers:
            out.append({"id": f"conflict:{cid}", "section": "conflict", "type": "single",
                        "required": True, "text": text, "options": _o(*options)})

    formal = _num(answers, "q6") >= 75
    if formal and (answers.get("q16") == "frequent" or answers.get("q14") == "strong"):
        add("formal_vs_local", "You chose a formal voice and frequent slang or strong local "
            "expression. Which matters more?",
            "formal|Stay formal; keep local expression light",
            "local|Relaxed and local; formality second",
            "mirror|Formal by default, relaxed when the customer is")
    if (_has(answers, "q3", "premium_adviser") or _has(answers, "q31", "premium")) \
            and answers.get("q9") == "frequently":
        add("premium_vs_emoji", "A premium voice with frequent emoji can read as off-brand. "
            "Which do you want?", "restrained|Restrained: rare emoji",
            "expressive|Expressive: keep frequent emoji")
    if _has(answers, "q1", "reserved") and _num(answers, "q10", 0) >= 70:
        add("reserved_vs_playful", "You described the brand as reserved but chose a playful "
            "humour level. Which is closer?", "reserved|Reserved, light humour at most",
            "playful|Playful")
    if answers.get("q7") == "one_two" and (answers.get("q11") == "structured"
                                          or answers.get("q12") == "explain"):
        add("brief_vs_detailed", "You chose 1–2 sentence answers and detailed explanations. "
            "Which should win?", "brief|Brief, with detail only when asked",
            "detailed|Detailed when the question needs it")
    if _num(answers, "q26", 0) >= 75 and answers.get("q28") == "never":
        add("proactive_vs_no_upsell", "You want proactive recommendations but no upselling. "
            "What should proactive mean?",
            "help|Suggest what fits their request, never extras",
            "upsell|Allow relevant extras")
    return out


# ── Calibration (§8) ─────────────────────────────────────────────────────────

SCENARIOS: list[dict[str, Any]] = [
    {"id": "product_enquiry", "title": "Product enquiry",
     "message": "Hi, do you have this in a bigger size?", "situation": "first_enquiry"},
    {"id": "price_objection", "title": "Price objection",
     "message": "Why is it so expensive? Can you reduce it?", "situation": "price_objection"},
    {"id": "interested_buyer", "title": "Interested buyer",
     "message": "I want to order two. How do I pay?", "situation": "buying_intent"},
    {"id": "angry_customer", "title": "Angry customer",
     "message": "I paid a week ago and my order still hasn't come. Nobody is replying me!",
     "situation": "complaint"},
    {"id": "returning_customer", "title": "Returning customer",
     "message": "Hello again! I bought from you last month. Do you have anything new?",
     "situation": "returning"},
    {"id": "unknown_question", "title": "Unknown question",
     "message": "Do you deliver to Ghana?", "situation": "uncertain_fact"},
]

SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}

# What each rating moves. Only the draft moves; the published profile never does.
FEEDBACK_ADJUSTMENTS: dict[str, dict[str, int]] = {
    "sounds_like_us": {},
    "too_formal": {"formality": -15, "warmth": 5},
    "too_casual": {"formality": 15, "humour": -10, "emoji": -10},
    "too_robotic": {"warmth": 10, "formality": -5, "enthusiasm": 5},
    "too_long": {"verbosity": -15},
    "too_short": {"verbosity": 15},
    "too_pushy": {"sales_assertiveness": -20, "proactivity": -10},
    "insufficient_empathy": {"empathy": 15},
}

FEEDBACK_LABELS = {
    "sounds_like_us": "Sounds like us", "too_formal": "Too formal", "too_casual": "Too casual",
    "too_robotic": "Too robotic", "too_long": "Too long", "too_short": "Too short",
    "too_pushy": "Too pushy", "insufficient_empathy": "Not enough empathy",
    "rewrite": "Rewrite it myself",
}
