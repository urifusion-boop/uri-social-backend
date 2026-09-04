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

Colour tokens (surface/field/ink/ink-quiet/accent/edge, §1.4) are
PLACEHOLDER values in tokens.py pending §10.1's reconciliation with the
28-style Visual Style Guides document — swap PLACEHOLDER_TOKENS for real
per-brand resolved hex values once that exists; nothing else about a format
module needs to change, since every format takes `tokens: dict` as an
explicit parameter rather than importing the placeholder directly.
"""
