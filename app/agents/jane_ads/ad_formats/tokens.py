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
    brand_mark: str = "optional"  # VSG-01-PROMPTS v2 §0 item 1 / §5 — "required" |
                                    # "optional" | "prohibited". Overrides the Brand
                                    # Overlay Spec's logo-on-by-default (v3 §1.5) for
                                    # formats a logo actively damages (Borrowed
                                    # Interface, News Headline, Humour/Cartoon) or
                                    # that need one to read as an ad at all (Review
                                    # Card, Receipt, Us vs Them, Testimonial + Offer,
                                    # Day1->Day30, Censored Item). Attribution always
                                    # comes from Meta's own Sponsored label + Page
                                    # name regardless of this value — nothing here
                                    # governs whether the ad is attributed, only
                                    # whether a logo belongs in frame.


from typing import Dict, List as _List, Optional  # noqa: E402 (kept near the dataclass above)


def logo_badge_layers(
    brand_logo_url: Optional[str], canvas_width: int, canvas_height: int, z_start: int,
) -> tuple[_List[Dict], int]:
    """A small bottom-right logo badge — white rounded backing (for
    legibility over any content underneath) + the brand's real logo image —
    used by every format whose own build_document() doesn't already reserve
    dedicated header space for a brand mark (Receipt and Review Card do
    their own top-of-canvas placement; this is the shared fallback for the
    rest). Returns ([], z_start) unchanged when there's no real logo to
    place — never fabricates one, matching every other brand_logo_url
    caller's own contract. Purely additive (drawn last, on top of existing
    content) so it never risks the careful per-format wrap/overflow math
    each layout already has tuned.
    """
    if not brand_logo_url:
        return [], z_start
    badge_w, badge_h, pad, margin = 140, 56, 10, 24
    x = canvas_width - badge_w - margin
    y = canvas_height - badge_h - margin
    z = z_start
    layers = [
        {
            "type": "shape", "z_index": z + 1, "shape": "rounded_rect",
            "x": x - pad, "y": y - pad, "width": badge_w + 2 * pad, "height": badge_h + 2 * pad,
            "corner_radius": 12, "fill_color": "#FFFFFFE6",
        },
        {
            "type": "brand_asset", "z_index": z + 2,
            "url": brand_logo_url, "x": x, "y": y, "width": badge_w, "height": badge_h,
        },
    ]
    return layers, z + 2
