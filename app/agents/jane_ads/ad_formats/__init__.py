"""
VSG-01 v3 static ad format library.

Each format module (receipt.py, ...) exposes:
  - a FORMAT: AdFormatDef constant describing it (§4's attribute schema, the
    asset_source it needs, and the Requirement gates from
    app.agents.jane_ads.entities/retrieval that must be met before it's
    offered)
  - a build_document(...) function that slot-fills the format's layout into
    a DocumentRendererService-shaped layered-document dict (§2's
    "Composition" for that format)
  - a render(...) async wrapper that calls
    DocumentRendererService.render_to_png(build_document(...)) directly, so
    a caller never has to know the layered-document shape itself

Colour tokens (surface/field/ink/ink-quiet/accent/edge, §1.4) default to
PLACEHOLDER_TOKENS in tokens.py; brand_tokens.resolve_brand_tokens() derives
the real per-brand `accent` value from brand_profiles.brand_colors — see
tokens.py's own docstring for why §10.1's expected reconciliation document
didn't turn out to define anything to reconcile against. Nothing about a
format module needs to change either way, since every format takes
`tokens: dict` as an explicit parameter rather than importing a fixed set
directly.

legibility.py's check_legibility()/assert_legible() implement §1.6's
compression test as an automated pre-render check against a built document
— every format module should be checked against it before shipping.
"""
