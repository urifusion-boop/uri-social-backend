"""
Attribute tagging — VSG-01 v3 §4.

"Every asset produced through this library is tagged at generation... This
is how format selection stops being a hypothesis. Which format converts
for which business type at which tier is the corpus inversion applied to
creative. Without tagging, the library remains 12 assertions." Also how
§1.10's aesthetic_register/polish-dial question resolves — same tag, same
outcome join, one investigation.

§4's schema:

    ad_format_attributes {
      format_id           : SEED-0xx
      asset_source        : upload_as_is | recomposite | generate | drawn
      layers_used         : L4 | L2-L4 | L2-L3-L4
      aesthetic_register  : polished | clean_simple | phone_native
      has_person          : bool
      person_is_real      : bool
      text_density        : none | minimal | moderate | heavy
      shows_price         : bool
      requires_isolation  : bool
    }

Four of these fields were deliberately excluded from AdFormatDef back in
tokens.py (see its own docstring): format_id/asset_source/layers_used/
requires_isolation are static per format and already live there;
has_person/person_is_real/text_density/shows_price are facts about a
*specific rendered asset*, not the format definition, and can't be read
off a static dataclass. build_ad_format_attributes() is where those two
halves join: the static FORMAT plus the real, already-built document
(text_density and shows_price are inferred directly from its text layers
— genuinely computable, not guessed) plus has_person/person_is_real,
which this module cannot see for itself (a generated scene may or may not
depict a person depending on the specific prompt used at render time; a
real uploaded photo's content isn't visible from a URL) and which the
caller must supply.

aesthetic_register defaults per format from what each format's own Layer
2 prompt (or lack of one) actually specifies — §1.10: "shot on a phone
camera and not retouched" prompts are the phone_native end; the Censored
Item's "dramatic single-source side lighting... studio product
photography" is the polished end; formats with no photographic register
of their own (drawn tables/comparisons, or a format whose subject is
whatever real photo a caller supplies) default to clean_simple, the
neutral middle. These are starting points, not fixed truths — a caller
building a specific asset with a specific brand voice may know better
than this default and should pass their own aesthetic_register.

"Joined to campaign outcome on campaign_id via the campaign_outcome event
(ASC-ENG-01 §6.1)" is an external analytics event this module has no
visibility into (no such event-emission pipeline is wired up in this
codebase yet) — build_ad_format_attributes() produces the payload shape
that event will eventually carry; actually emitting it is a later
integration point once a live ad-generation call site exists (VSG-01
steps 7-9 predate one — see every format module's own "not yet wired
into a live call path" note).
"""
import re
from typing import Any, Dict, Optional

from .tokens import AdFormatDef

_AESTHETIC_REGISTER_BY_FORMAT: Dict[str, str] = {
    "SEED-081": "clean_simple",   # The Receipt — drawn, unstyled
    "SEED-075": "clean_simple",   # Us vs Them — drawn
    "SEED-087": "phone_native",   # Borrowed Interface — a phone chat UI by definition
    "SEED-078": "clean_simple",   # Day 1 → Day 30 — register follows the caller's real photos
    "SEED-093": "clean_simple",   # Review Card — register follows the caller's real photo
    "SEED-080": "phone_native",   # Problem/Solution — "shot on a phone camera... unstyled"
    "SEED-074": "phone_native",   # Testimonial + Offer — no-person path's own prompt is phone_native
    "SEED-082": "clean_simple",   # Text on a Face — register follows the caller's real photo
    "SEED-077": "phone_native",   # News Headline — "authentic and unstyled" documentary reportage
    "SEED-083": "polished",       # The Censored Item — "dramatic... studio product photography"
    "SEED-088": "clean_simple",   # Starter Pack — "clean minimal styling"
    "SEED-089": "clean_simple",   # Humour/Cartoon — "clean simple linework" (illustration, not
                                   # strictly on this photographic scale — best fit, not a perfect one)
}

_TEXT_DENSITY_WORD_BUCKETS = (
    (0, "none"),
    (10, "minimal"),
    (30, "moderate"),
)  # anything above the last bucket's threshold is "heavy"

# Matches an actual currency amount in rendered copy — not just any accent-
# coloured text, since accent is also used for non-price emphasis (Review
# Card's star row, for one) and a colour-based check would conflate the two.
_PRICE_PATTERN = re.compile(r"₦\s*[\d,]+|\bN\d[\d,]*\b")


def default_aesthetic_register(format_id: str) -> str:
    return _AESTHETIC_REGISTER_BY_FORMAT.get(format_id, "clean_simple")


def _text_layer_contents(document: Dict[str, Any]):
    return [
        str(layer.get("content", ""))
        for layer in document.get("layers", [])
        if layer.get("type") == "text" and layer.get("content")
    ]


def infer_text_density(document: Dict[str, Any]) -> str:
    word_count = sum(len(content.split()) for content in _text_layer_contents(document))
    for threshold, label in _TEXT_DENSITY_WORD_BUCKETS:
        if word_count <= threshold:
            return label
    return "heavy"


def infer_shows_price(document: Dict[str, Any]) -> bool:
    return any(_PRICE_PATTERN.search(content) for content in _text_layer_contents(document))


def build_ad_format_attributes(
    format_def: AdFormatDef,
    document: Dict[str, Any],
    has_person: bool,
    person_is_real: bool,
    aesthetic_register: Optional[str] = None,
) -> Dict[str, Any]:
    """
    format_def: the format's own static AdFormatDef (format_id,
    asset_source, layers_used, requires_isolation).
    document: the actual built document (build_document()'s return value)
    — text_density and shows_price are computed from its real text layers.
    has_person / person_is_real: facts about this specific asset that
    can't be inferred from the document alone — see module docstring.
    aesthetic_register: defaults per format_id (default_aesthetic_
    register) if not given.
    """
    return {
        "format_id": format_def.format_id,
        "asset_source": format_def.asset_source,
        "layers_used": format_def.layers_used,
        "aesthetic_register": aesthetic_register or default_aesthetic_register(format_def.format_id),
        "has_person": has_person,
        "person_is_real": person_is_real,
        "text_density": infer_text_density(document),
        "shows_price": infer_shows_price(document),
        "requires_isolation": format_def.requires_isolation,
    }
