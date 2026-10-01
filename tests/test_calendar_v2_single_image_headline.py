"""_single_image_seed_with_headline (content_calendar_v2_router.py) — the fix
for single-image content-calendar posts intermittently generating with no
headline text at all, while carousel slides from the same feature always
rendered text correctly.

Root cause: image_seed_content always ends with the upstream ai_image_prompt's
own "no text, no logos" instruction. The carousel branch overrides that by
appending each slide's real headline/body AFTER it; the single-image branch
never did the equivalent. Pure function, no I/O — a direct unit test exercises
the real logic, matching this codebase's existing convention for isolated
prompt-building helpers (see test_per_platform_style_resolution.py)."""
from app.agents.content_calendar_v2.routers.content_calendar_v2_router import (
    _single_image_seed_with_headline,
)


class TestSingleImageSeedWithHeadline:
    def test_appends_headline_after_the_no_text_instruction(self):
        base = "Some post idea.\nImage direction: a tidy desk photo. no text, no logos."
        result = _single_image_seed_with_headline(base, {"headline": "Reclaim Your Time"})
        assert result == f"{base}. Headline: Reclaim Your Time"
        # The headline must be the LAST thing in the string — the image model
        # treats the most-recent instruction as authoritative, which is the
        # entire mechanism this fix relies on.
        assert result.endswith("Reclaim Your Time")

    def test_no_headline_leaves_seed_content_unchanged(self):
        base = "Some post idea. no text, no logos."
        assert _single_image_seed_with_headline(base, {}) == base
        assert _single_image_seed_with_headline(base, {"headline": ""}) == base
        assert _single_image_seed_with_headline(base, {"headline": None}) == base

    def test_missing_exact_copy_fields_dont_crash(self):
        # exact_copy on an item can legitimately be a bare dict with no
        # "headline" key at all (older calendar items, or a format where the
        # LLM didn't produce one) — must degrade to "no override", not raise.
        result = _single_image_seed_with_headline("base seed", {"caption": "unrelated"})
        assert result == "base seed"
