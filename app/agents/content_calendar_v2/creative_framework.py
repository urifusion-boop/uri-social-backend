# app/agents/content_calendar_v2/creative_framework.py
"""
Content Calendar V2 — Creative Content Framework (PRD "URI Social — Living
Content Calendar & Creative Intelligence Engine", §4-8, §35).

Plain versioned config, deliberately NOT a Mongo-backed/human-approved record
system (evaluated jane_ads' VSG-01 corpus as a precedent and rejected it —
too heavy: immutable individually-approved Strategy records, zero diversity
checking, built for a different problem). This mirrors how v1's own
POST_FORMATS/HOOK_STYLES already live as plain Python constants
(content_calendar_service.py) — the PRD's own §35 asks for exactly that: "a
versioned configuration layer", editable without rewriting application logic.

Zero Mongo dependency, zero LLM calls. Imported directly into
content_calendar_v2_service.py's generation pipeline the same way
POST_FORMATS is imported today.

Bump FRAMEWORK_VERSION whenever TERRITORIES/ANGLES/CREATIVE_DEVICES/
VALIDATION_RULES change — every generated plan stores whichever version was
live at generation time (PRD §51), so behavior stays explainable/rollback-able
via normal git history on this file, without needing an in-app version-
picker UI.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

FRAMEWORK_VERSION = "2026-09-v1"


# ── Layer 1 — Content Territories (PRD §5, Territories A-L) ────────────────
# Each territory: a label + generic subjects any business can draw from.
# The engine does not need to use every territory — it selects those
# relevant to the business (PRD §5).

TERRITORIES: Dict[str, Dict[str, Any]] = {
    "A_PROBLEM": {
        "label": "Problem",
        "description": "Explore customer problems.",
        "subjects": [
            "Mistake", "Hidden problem", "Expensive problem", "Overlooked issue",
            "Common failure", "Customer frustration",
            "What happens when the problem is ignored", "Why the obvious solution fails",
            "Industry problem", "Customer misconception",
        ],
    },
    "B_DESIRE": {
        "label": "Desire",
        "description": "Explore what the audience wants.",
        "subjects": [
            "Dream outcome", "Transformation", "Convenience", "Lifestyle", "Status",
            "Freedom", "Speed", "Confidence", "Success", "Comfort", "Identity",
            "Aspiration", "What life looks like when...",
        ],
    },
    "C_CURIOSITY": {
        "label": "Curiosity",
        "description": "Create interest through information gaps.",
        "subjects": [
            "Things people don't know", "Hidden facts", "Unexpected facts", "Secrets",
            "Behind-the-scenes information", "What happens before...", "What happens after...",
            "What customers rarely see", "Unexpected explanation", "Nobody tells you...",
        ],
    },
    "D_CONTRARIAN": {
        "label": "Contrarian",
        "description": (
            "Challenge assumptions. Claims must be grounded in facts or genuine "
            "business perspective — never manufacture controversy (PRD §5)."
        ),
        "subjects": [
            "Unpopular opinion", "Industry misconception", "Common advice that is incomplete/wrong",
            "Stop doing...", "You don't actually need...", "More isn't always better",
            "Alternative approach", "Founder perspective", "Industry disagreement",
        ],
    },
    "E_PROOF": {
        "label": "Proof",
        "description": "Demonstrate credibility. Only real evidence available to the business (PRD §5, §31).",
        "subjects": [
            "Testimonial", "Customer result", "Review", "Case study", "Before/after",
            "Screenshot", "Demonstration", "Customer story", "Process evidence",
            "Results", "Social proof",
        ],
    },
    "F_PEOPLE": {
        "label": "People",
        "description": "Humanize the company.",
        "subjects": [
            "Founder", "Employee", "Customer", "Team", "Expert", "Personality",
            "Founder belief", "Founder story", "Team culture", "Customer reaction",
            "Day in the life", "Origin story",
        ],
    },
    "G_PROCESS": {
        "label": "Process",
        "description": "Show how the business works.",
        "subjects": [
            "How it's made", "How it's delivered", "Behind the scenes", "Quality control",
            "Preparation", "Packaging", "Installation", "Research", "Customer onboarding",
            "Service delivery", "Decision process", "What happens before delivery",
        ],
    },
    "H_COMPARISON": {
        "label": "Comparison",
        "description": "Help the audience make decisions.",
        "subjects": [
            "A vs B", "Before vs after", "Old vs new", "DIY vs professional",
            "Cheap vs suitable", "Option A vs Option B", "Beginner vs expert",
            "Wrong vs right", "What customers think vs reality", "Which is right for you?",
        ],
    },
    "I_EDUCATION": {
        "label": "Education",
        "description": (
            "Teach something useful. Must be specific — avoid generic formulations "
            "like '5 tips for success' (PRD §5, checked by _anti_boring_check)."
        ),
        "subjects": [
            "How-to", "Checklist", "FAQ", "Framework", "Mistakes", "Buying guide",
            "Decision guide", "Explanation", "Definitions", "Common misunderstandings",
            "Calculation", "Before you buy...",
        ],
    },
    "J_CULTURE_CONTEXT": {
        "label": "Culture / Context",
        "description": "Use the world around the audience. Only when context genuinely improves the content (PRD §5).",
        "subjects": [
            "Nigerian behaviour", "Local habits", "Cultural observations", "Seasonal behaviour",
            "Local humour", "Industry culture", "Community behaviour", "Public moments",
            "Local conversations", "Nigerian-specific customer behaviour",
        ],
    },
    "K_ENTERTAINMENT": {
        "label": "Entertainment",
        "description": "Create content people want to consume.",
        "subjects": [
            "POV", "Skit", "Meme", "Reaction", "Humour", "Debate", "Prediction", "Game",
            "This-or-that", "Ranking", "Challenge", "Relatable situation",
        ],
    },
    "L_COMMERCIAL": {
        "label": "Commercial",
        "description": "Ask for the business. Must not dominate the whole calendar (PRD §5, §8).",
        "max_share": 0.20,  # PRD §8 "must not dominate" — the only territory with a hard ceiling
        "subjects": [
            "Product", "Service", "Offer", "Launch", "Pricing", "Product demonstration",
            "Feature to benefit", "Why buy", "Who it's for", "Why now", "Availability",
            "Objection handling", "Bundle", "Consultation", "Booking", "Purchase",
        ],
    },
}

# ── Layer 2 — Subject Library narrowing (PRD §6) ────────────────────────────
# Per-industry subject hints that narrow the generic territory subjects above
# toward industry-specific ones (same pattern as v1's INDUSTRY_MIX). Matched
# by substring against brand.industry, same convention as v1's _pick_mix.
INDUSTRY_SUBJECT_HINTS: Dict[str, List[str]] = {
    "solar": [
        "Electricity reliability", "Generator dependence", "Upfront solar cost",
        "Financing", "Battery", "Inverter", "Installation", "Load requirements",
        "Maintenance", "Customer affordability",
    ],
    "school": [
        "Parent anxiety", "Sixth-form selection", "Career direction",
        "University preparation", "Academic support", "Student independence", "Subject choice",
    ],
    "education": [
        "Parent anxiety", "Career direction", "Academic support", "Student independence",
    ],
    "restaurant": [
        "Food quality", "Dining experience", "Convenience", "Menu choice",
        "Restaurant atmosphere", "Chef process", "Customer expectations",
    ],
    "food": [
        "Food quality", "Freshness", "Ingredient sourcing", "Convenience", "Menu choice",
    ],
    "beauty": [
        "Ingredient quality", "Results timeline", "Skin/hair type fit", "Routine simplicity",
        "Authenticity of claims",
    ],
    "skincare": [
        "Ingredient quality", "Results timeline", "Skin type fit", "Routine simplicity",
        "Authenticity of claims",
    ],
    "fashion": [
        "Fit", "Fabric quality", "Styling versatility", "Occasion appropriateness",
        "Trend longevity",
    ],
    "real estate": [
        "Location value", "Financing/mortgage", "Documentation trust", "Property condition",
        "Investment return",
    ],
    "fintech": [
        "Transaction reliability", "Security/trust", "Fee transparency", "Speed",
        "Regulatory compliance",
    ],
    "saas": [
        "Onboarding friction", "Integration ease", "ROI/time-saved", "Support quality",
        "Feature depth vs simplicity",
    ],
    "technology": [
        "Onboarding friction", "Reliability", "Support quality", "Ease of use",
    ],
    "healthcare": [
        "Access/wait time", "Trust in provider", "Cost transparency", "Outcome confidence",
    ],
    "professional services": [
        "Expertise proof", "Process transparency", "Cost predictability", "Outcome confidence",
    ],
}


def _subjects_for_territory(territory_key: str, industry: str) -> List[str]:
    """Generic territory subjects, extended with industry-narrowed ones when
    the industry matches an INDUSTRY_SUBJECT_HINTS key (substring match, same
    convention as v1's _pick_mix). Both are offered — narrowing extends the
    pool, it doesn't replace it, since not every territory's generic subject
    needs an industry-specific variant."""
    territory = TERRITORIES[territory_key]
    subjects = list(territory["subjects"])
    industry_lower = (industry or "").lower()
    for hint_key, hint_subjects in INDUSTRY_SUBJECT_HINTS.items():
        if hint_key in industry_lower:
            subjects = subjects + hint_subjects
            break
    return subjects


# ── Layer 3 — Angle Library (PRD §7) — universal, not industry-narrowed ────
ANGLES: List[Dict[str, str]] = [
    {"key": "the_mistake", "label": "The Mistake"},
    {"key": "the_hidden_cost", "label": "The Hidden Cost"},
    {"key": "the_misconception", "label": "The Misconception"},
    {"key": "the_unpopular_opinion", "label": "The Unpopular Opinion"},
    {"key": "the_customer_story", "label": "The Customer Story"},
    {"key": "the_transformation", "label": "The Transformation"},
    {"key": "the_comparison", "label": "The Comparison"},
    {"key": "the_unexpected_truth", "label": "The Unexpected Truth"},
    {"key": "the_beginner_perspective", "label": "The Beginner Perspective"},
    {"key": "the_expert_perspective", "label": "The Expert Perspective"},
    {"key": "the_founder_perspective", "label": "The Founder Perspective"},
    {"key": "the_customers_question", "label": "The Customer's Question"},
    # Single quotes deliberately, not double — confirmed live: a label with an
    # embedded double-quote (e.g. The "Before You Buy" Angle) that the model
    # is asked to echo back verbatim inside a JSON string value gets its
    # internal quotes escaped inconsistently, producing malformed JSON
    # ("Expecting ',' delimiter" parse failures on every candidate chunk).
    {"key": "before_you_buy", "label": "The 'Before You Buy' Angle"},
    {"key": "nobody_tells_you", "label": "The 'Nobody Tells You' Angle"},
    {"key": "what_happens_if", "label": "The 'What Happens If' Angle"},
    {"key": "what_would_you_choose", "label": "The 'What Would You Choose?' Angle"},
    {"key": "the_myth", "label": "The Myth"},
    {"key": "the_challenge", "label": "The Challenge"},
    {"key": "the_experiment", "label": "The Experiment"},
    {"key": "the_decision_guide", "label": "The Decision Guide"},
    {"key": "the_story", "label": "The Story"},
    {"key": "the_confession", "label": "The Confession"},
    {"key": "the_reaction", "label": "The Reaction"},
    {"key": "the_explanation", "label": "The Explanation"},
    {"key": "the_warning", "label": "The Warning"},
    {"key": "the_opportunity", "label": "The Opportunity"},
    {"key": "the_aspiration", "label": "The Aspiration"},
]
ANGLE_KEYS = {a["key"] for a in ANGLES}


# ── Layer 4 — Creative Device Library (PRD §8), 5 categories ───────────────
CREATIVE_DEVICES: Dict[str, List[Dict[str, str]]] = {
    "story": [
        {"key": "founder_story", "label": "Founder story"},
        {"key": "customer_story", "label": "Customer story"},
        {"key": "transformation", "label": "Transformation"},
        {"key": "confession", "label": "Confession"},
        {"key": "revelation", "label": "Revelation"},
        {"key": "journey", "label": "Journey"},
        {"key": "case_study", "label": "Case study"},
    ],
    "visual": [
        {"key": "split_screen", "label": "Split screen"},
        {"key": "before_after", "label": "Before/after"},
        {"key": "side_by_side", "label": "Side-by-side"},
        {"key": "product_hero", "label": "Product hero"},
        {"key": "close_up", "label": "Close-up"},
        {"key": "unexpected_environment", "label": "Unexpected environment"},
        {"key": "large_object_metaphor", "label": "Large object metaphor"},
        {"key": "photo_collage", "label": "Photo collage"},
        {"key": "screenshot_led", "label": "Screenshot-led"},
        {"key": "editorial_design", "label": "Editorial design"},
        {"key": "documentary_style", "label": "Documentary style"},
        {"key": "minimal_typography", "label": "Minimal typography"},
        {"key": "visual_comparison", "label": "Visual comparison"},
    ],
    "conversational": [
        {"key": "talking_head", "label": "Talking head"},
        {"key": "interview", "label": "Interview"},
        {"key": "street_question", "label": "Street question"},
        {"key": "customer_question", "label": "Customer question"},
        {"key": "founder_opinion", "label": "Founder opinion"},
        {"key": "debate", "label": "Debate"},
        {"key": "reaction", "label": "Reaction"},
        {"key": "qanda", "label": "Q&A"},
    ],
    "psychological": [
        {"key": "curiosity", "label": "Curiosity"},
        {"key": "recognition", "label": "Recognition"},
        {"key": "surprise", "label": "Surprise"},
        {"key": "desire", "label": "Desire"},
        {"key": "fear_of_mistake", "label": "Fear of mistake"},
        {"key": "loss_aversion", "label": "Loss aversion"},
        {"key": "social_proof", "label": "Social proof"},
        {"key": "status", "label": "Status"},
        {"key": "urgency", "label": "Urgency"},
        {"key": "identity", "label": "Identity"},
        {"key": "aspiration", "label": "Aspiration"},
    ],
    "structural": [
        {"key": "myth_vs_fact", "label": "Myth vs fact"},
        {"key": "a_vs_b", "label": "A vs B"},
        {"key": "problem_solution", "label": "Problem to solution"},
        {"key": "question_answer", "label": "Question to answer"},
        {"key": "before_after_structural", "label": "Before to after"},
        {"key": "checklist", "label": "Checklist"},
        {"key": "ranking", "label": "Ranking"},
        {"key": "challenge_structural", "label": "Challenge"},
        {"key": "test", "label": "Test"},
        {"key": "timeline", "label": "Timeline"},
        {"key": "decision_tree", "label": "Decision tree"},
    ],
}

# Carousel slide count picked by structural device — the smallest number
# that communicates the idea clearly (PRD §17), never padded to the max.
# 2 = simple comparison, 3 = hook->insight->CTA, 4 = hook->problem->solution->CTA,
# 5 = hook->context->problem->solution->CTA.
STRUCTURAL_DEVICE_SLIDE_HINT: Dict[str, int] = {
    "a_vs_b": 2,
    "myth_vs_fact": 3,
    "question_answer": 3,
    "problem_solution": 4,
    "before_after_structural": 4,
    "checklist": 4,
    "decision_tree": 5,
    "timeline": 5,
    "ranking": 4,
    "test": 3,
    "challenge_structural": 3,
}


# ── Validation rules (PRD §14-15, §28) — deterministic, code-enforced ──────
VALIDATION_RULES: Dict[str, Any] = {
    "item_count": 30,
    "carousel_slide_range": (2, 5),
    "commercial_territory_max_share": TERRITORIES["L_COMMERCIAL"]["max_share"],
    # PRD §15 — minimums, not fixed percentages. Keys are the "creative
    # buckets" a plan must contain at least one of, mapped from territory/
    # device combinations in the selection stage (_select_diverse_thirty).
    "min_creative_buckets": {
        "story_driven": 1,
        "proof_social_proof": 1,
        "behind_the_scenes_process": 1,
        "customer_problem": 1,
        "audience_interaction": 1,
        "commercial": 1,
        "opinion_contrarian": 1,
        "visually_distinctive": 1,
        "unexpected_experimental": 1,
    },
}

# PRD §30 — generic AI phrasings that should trigger a "creative quality
# review" note, never an auto-reject (execution can still redeem a generic
# opener).
ANTI_BORING_PHRASES: List[str] = [
    "here are 5 tips",
    "here are 3 reasons",
    "did you know",
    "we are excited to announce",
    "in today's fast-paced world",
    "looking for the best",
    "at {brand}, we believe",  # brand-name placeholder checked separately
    "we believe",
]


def get_creative_framework(industry: str) -> Dict[str, Any]:
    """The one exported function — Step 2 of the pipeline (PRD §35). Returns
    the framework version, every territory with its subjects narrowed toward
    the given industry where a hint exists, and the universal angle/device
    libraries (these stay industry-agnostic per PRD §6-8)."""
    territories = {
        key: {
            "label": t["label"],
            "description": t["description"],
            "subjects": _subjects_for_territory(key, industry),
            **({"max_share": t["max_share"]} if "max_share" in t else {}),
        }
        for key, t in TERRITORIES.items()
    }
    return {
        "framework_version": FRAMEWORK_VERSION,
        "territories": territories,
        "angles": ANGLES,
        "creative_devices": CREATIVE_DEVICES,
        "validation_rules": VALIDATION_RULES,
    }
