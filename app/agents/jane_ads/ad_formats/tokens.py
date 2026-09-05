"""
Colour token roles (VSG-01 v3 §1.4) — PLACEHOLDER values.

§10.1 named reconciling these role names with "the 28-style Visual Style
Guides document" the largest outstanding dependency. That document turned
out to be app/agents/social_media_manager/services/style_library.py itself
(the "28" is stale — confirmed with the person who wrote VSG-01 v3, the
library has simply grown since) — but it carries no tokenised colour-role
system under any name; every entry is prose meant to steer Layer 2 AI
generation, not a role-to-hex mapping. The real per-brand resolution path
is brand_tokens.resolve_brand_tokens(), built against brand_profiles'
actual brand_colors field — see that module's docstring. Only `accent`
varies by brand; the other five roles stay exactly what's defined here,
recognisable as URI's own brand pink (#CD1B78, the same accent used
throughout the frontend's Playbook UI) rather than an arbitrary colour, so
a placeholder-token render still looks intentional rather than broken.

Every format's build_document(...) takes `tokens: dict` as an explicit
parameter (defaulting to PLACEHOLDER_TOKENS) rather than importing this
module's constant directly inside the layout logic — the caller passes
resolve_brand_tokens(...)'s result instead and nothing in a format
module's own code needs to change.
"""
from dataclasses import dataclass, field
from typing import List

PLACEHOLDER_TOKENS = {
    "surface": "#FAF7F2",     # base field the asset sits on
    "field": "#FFFFFF",       # secondary block holding quote/offer copy
    "ink": "#1A1A1A",         # primary type colour
    "ink-quiet": "#575450",   # attribution, disclaimers, secondary lines — same
                               # warm-grey hue as an earlier #7A7570, darkened:
                               # that value measured at only 4.27:1/4.56:1 against
                               # surface/field, failing §1.6's 7:1 floor (found by
                               # legibility.py's contrast check, not assumed safe).
                               # #575450 clears 7.05:1/7.53:1 against both.
    "accent": "#CD1B78",      # price, offer band, star row, single emphasis
    "edge": "#D8D2C8",        # rules, dividers, receipt leaders, borders
}


@dataclass
class AdFormatDef:
    """
    §4's attribute schema, minus the fields only known at generation time
    (has_person/person_is_real/text_density/shows_price — those come from
    the actual filled slots, not the format definition itself).
    """
    format_id: str            # e.g. "SEED-081"
    name: str                 # "The Receipt"
    asset_source: str         # CreativeSource value — "drawn"/"upload_as_is"/etc (kept
                               # as a plain str here, not the CreativeSource enum, so
                               # this module has no import-order dependency on
                               # app.agents.jane_ads.models; callers that need the
                               # enum can construct CreativeSource(asset_source))
    layers_used: str          # "L4" | "L2-L4" | "L2-L3-L4"
    requires: List[str] = field(default_factory=list)  # Requirement values (§6 retrieval gate)
    requires_isolation: bool = False  # SEED-079 usage cap
