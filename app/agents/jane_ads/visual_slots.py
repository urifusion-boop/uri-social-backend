"""
Zone A/B for Layer 2 visual prompts, and the §3 slot vocabulary — VSG-01
v3 §1.3/§3.

`creative.py` already enforces Zone A/B for ad COPY: `_zone_a_block`,
`_leakage_terms`, `_check_leakage`, `_strip_leaked_terms` keep targeting
parameters (geo_target, audience_segment, interest_category, geo_pockets)
out of headlines and body text — the observed bug was `"Lekki creatives"`,
a geo-target concatenated straight into a headline.

§1.3 extends the identical prohibition to Layer 2 image-generation prompts:
"`A bag on a table in Lekki` is the identical error arriving through the
visual instruction rather than the copy." There was no equivalent guard for
that code path — this module is it, built parallel to (not by editing)
creative.py's copy-side guard, since a scene-description prompt and an ad
headline are different shapes of text with the same underlying rule.

Two layers of defence, matching §5's framing of items 5-13 as "automated
blocks, not review prompts" — on the ₦15k tier no human sees the prompt or
the asset before it ships:

1. **Structural (§3, "enumerating them prevents Layer 2 drifting to
   foreign defaults")** — `{{nigerian_setting}}`, lighting, and
   `{{customer_description}}`'s dress register are closed vocabularies.
   `resolve_nigerian_setting`/`resolve_lighting` reject anything not in the
   enumerated list, and `build_customer_description`'s signature has no
   `audience_segment` parameter at all — the bug §3 names ("never populated
   from `audience_segment`") is not just discouraged, there is no argument
   to populate it from.

2. **Leakage catch-all** — `check_visual_leakage`/`assert_no_visual_leakage`
   scan a fully-assembled Layer 2 prompt string for Zone A terms that
   shouldn't be there regardless of how they got in (a slot value, a
   hand-written fragment, string concatenation upstream of this module).
   Fails closed (raises) rather than silently stripping and shipping a
   scene prompt with a chunk edited out of it — stripping text out of ad
   copy can be smoothed by a human or a regenerate; stripping words out of
   an image-generation prompt risks leaving a broken instruction the model
   has to guess at, which is worse than blocking.

Not yet wired into a live call path: there is no Layer 2 prompt-building
orchestrator in this codebase yet (VSG-01's generation-dependent L2
formats are step 7, not yet built). These are the primitives that step
will call.
"""
from typing import Dict, List, Optional

# §3: "a scene descriptor drawn from the slot vocabulary, not a resolved
# geo-target. It describes a kind of place, never the targeting parameter."
NIGERIAN_SETTINGS = (
    "a Lagos street with informal shopfronts",
    "a small tiled shop interior",
    "an open-air market stall",
    "a compound courtyard",
    "a tailoring workshop",
    "a modern Lagos office interior",
    "a residential estate gate",
    "a roadside food stand",
)

# §3: "No Northern-hemisphere lighting language."
LIGHTING_DEFAULTS = (
    "strong equatorial daylight",
    "warm late-afternoon light",
    "overcast diffused light",
    "shaded interior with daylight from a doorway",
)

# §3: dress register options for {{customer_description}}.
DRESS_REGISTERS = ("casual", "workwear", "formal", "traditional")

# §1.3's own table — Zone A never appears in a prompt or on the asset;
# Zone B may, because the customer needs it. Kept here as documentation and
# for tests; the actual mechanical check is `check_visual_leakage` against
# the Zone A values collected by `visual_leakage_terms`, not a lookup
# against these tuples directly.
ZONE_A_FIELDS = ("geo_target", "audience_segment", "interest_categories", "platform", "objective", "budget")
ZONE_B_FIELDS = ("service_area", "who_its_for", "price", "the_action")


class InvalidSlotValue(ValueError):
    """§3: the slot vocabulary is enumerated specifically to prevent Layer 2
    drifting to foreign defaults, or a slot being filled from a Zone A
    field. Raised instead of silently accepting free text."""
    pass


class VisualLeakageDetected(ValueError):
    """§1.3/§5 item 9: a Zone A value (or a variant's geo_pocket) appears in
    an assembled Layer 2 prompt. This is one of the pre-flight checklist's
    automated blocks — fails closed rather than shipping a prompt with a
    targeting parameter baked in."""
    pass


def resolve_nigerian_setting(setting: str) -> str:
    """§3: never populate this from `geo_target` — that's the exact bug
    this document names. Accepting only the enumerated list makes the bug
    a ValueError instead of a silent substitution."""
    if setting not in NIGERIAN_SETTINGS:
        raise InvalidSlotValue(
            f"{setting!r} is not in the §3 slot vocabulary for "
            f"{{{{nigerian_setting}}}} — must be one of {NIGERIAN_SETTINGS}"
        )
    return setting


def resolve_lighting(lighting: str) -> str:
    if lighting not in LIGHTING_DEFAULTS:
        raise InvalidSlotValue(
            f"{lighting!r} is not one of §3's lighting defaults — "
            f"must be one of {LIGHTING_DEFAULTS}"
        )
    return lighting


def resolve_surface_hex(tokens: Dict[str, str]) -> str:
    """§3: "{{surface_hex}} — resolved hex from the client's token set.
    Never a colour name." Trivial by design: the enforcement is that a
    Layer 2 prompt-builder calls this instead of hardcoding a hex or
    naming a colour inline. Pairs with brand_tokens.resolve_brand_tokens()
    for the actual per-brand value."""
    return tokens["surface"]


def build_customer_description(age_range: str, dress_register: str, gender: str = "") -> str:
    """§3: age range, gender where relevant, dress register — "Always with
    skin tone and West African features stated. Never populated from
    `audience_segment`." There is deliberately no `audience_segment`
    parameter here — the bug §3 names isn't just discouraged, there's no
    argument slot to put it in."""
    if dress_register not in DRESS_REGISTERS:
        raise InvalidSlotValue(
            f"{dress_register!r} is not a §3 dress register — "
            f"must be one of {DRESS_REGISTERS}"
        )
    parts = [age_range.strip()] if age_range and age_range.strip() else []
    if gender and gender.strip():
        parts.append(gender.strip())
    parts.append(f"{dress_register} dress")
    parts.append("deep brown to dark brown skin tones, West African features")
    return ", ".join(parts)


def visual_leakage_terms(
    geo_target: str = "",
    audience_segment: str = "",
    interest_categories: str = "",
    geo_pockets: Optional[List[str]] = None,
) -> List[str]:
    """The Zone A values that must never appear verbatim in an assembled
    Layer 2 prompt — mirrors creative.py's `_leakage_terms` for the copy
    side exactly, applied here to visual instruction instead (§1.3: "A
    targeting parameter must not enter a scene description any more than
    it enters a headline")."""
    terms = [t.strip() for t in (geo_target, audience_segment, interest_categories) if t and t.strip()]
    terms += [p.strip() for p in (geo_pockets or []) if p and p.strip()]
    return terms


def check_visual_leakage(prompt: str, forbidden_terms: List[str]) -> List[str]:
    """Deterministic, cheap check — case-insensitive substring match against
    the assembled prompt. Returns which specific terms leaked, if any."""
    lowered = prompt.lower()
    return [t for t in forbidden_terms if t.lower() in lowered]


def assert_no_visual_leakage(prompt: str, forbidden_terms: List[str]) -> None:
    """The automated block itself (§5 item 9) — raises rather than shipping
    a prompt with a targeting parameter in it. Deliberately no strip-and-
    continue fallback here (unlike creative.py's copy-side last resort):
    editing words out of a scene-description prompt can leave a broken
    instruction for the image model to guess at, which is worse than
    blocking and having the caller rebuild the prompt from clean slots."""
    leaked = check_visual_leakage(prompt, forbidden_terms)
    if leaked:
        raise VisualLeakageDetected(
            f"Zone A value(s) {leaked} found in Layer 2 prompt — a targeting "
            "parameter must not enter a scene description (VSG-01 §1.3)"
        )
