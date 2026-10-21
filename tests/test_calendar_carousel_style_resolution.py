"""
create_draft_from_item_v2's image-generation branch (content_calendar_v2_
router.py) spawns one _generate_image_bg task per carousel slide — its own
call site, separate from the main Create-tab flow in complete_social_
manager.py. It never called _resolve_carousel_shared_context, so every
slide resolved its own custom-guide/library-style/CTA choice independently:
the slide-0-writes-others-read race (unstyled slides 2+) AND the per-slide
library-style drift (a different style per slide) that
_resolve_carousel_shared_context was built to eliminate on the main flow
were both still present here. Confirmed live via a client-reported 3-slide
carousel with 3 different visual styles, created from the Content Calendar.

These are structural/source checks rather than a full endpoint test — the
endpoint itself is heavy to exercise directly (credits, DB, the content
generation service), and the one thing that matters here is call order:
_resolve_carousel_shared_context must run once per carousel draft, before
any of that draft's per-slide _generate_image_bg tasks are created, and
its result — not the raw brand_context — must be what every slide receives.
"""
import inspect

from app.agents.content_calendar_v2.routers import content_calendar_v2_router as cal_router


def test_calendar_router_imports_the_carousel_resolver():
    source = inspect.getsource(cal_router)
    assert "_resolve_carousel_shared_context" in source


def test_carousel_context_resolved_once_before_the_per_slide_loop():
    source = inspect.getsource(cal_router.create_draft_from_item_v2)
    resolve_idx = source.index("carousel_brand_context = await _resolve_carousel_shared_context")
    loop_idx = source.index("for slide_index, slide in enumerate(slides):")
    task_call_idx = source.index("brand_context=carousel_brand_context")
    assert resolve_idx < loop_idx < task_call_idx


def test_non_carousel_drafts_still_use_the_raw_brand_context():
    # The single-image branch (else: of `if item.get("format") == "carousel"`)
    # has no shared carousel-wide state to resolve — it must keep passing the
    # plain brand_context, not get coupled to the carousel resolver.
    source = inspect.getsource(cal_router.create_draft_from_item_v2)
    else_branch = source.split('if item.get("format") == "carousel":', 1)[1]
    non_carousel_part = else_branch.split("else:", 1)[1]
    assert "brand_context=brand_context,\n" in non_carousel_part


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
