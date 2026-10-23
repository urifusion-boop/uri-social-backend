"""
Carousel slide choice outside Create Post.

Customers asked for more than 5 slides and the same control everywhere:
Upload Content now takes a slide count and a per-slide image choice (like
Create Post), clamped to 2-10 (Instagram's carousel limit) since a manual
upload is the user's own photos, not an AI-generated plan.

Content Calendar v2's own override is narrower: a business rule (confirmed
live, Oct 2026) caps every calendar-sourced carousel at 3 slides regardless
of what the user picks in the Create Draft modal — the plan's own suggested
count already respected this; only the manual-override path didn't, until
now.
"""
import inspect

import pytest

from app.agents.social_media_manager.routers import complete_social_manager as csm
from app.agents.social_media_manager.routers.complete_social_manager import _upload_slide_image_indices


class TestUploadSlideImageIndices:
    def test_no_choice_is_one_slide_per_upload(self):
        assert _upload_slide_image_indices(3) == [0, 1, 2]

    def test_more_slides_than_images_cycles_uploads(self):
        assert _upload_slide_image_indices(3, 8) == [0, 1, 2, 0, 1, 2, 0, 1]

    def test_fewer_slides_than_images_uses_first_ones(self):
        assert _upload_slide_image_indices(5, 3) == [0, 1, 2]

    def test_manual_map_picks_image_per_slide(self):
        assert _upload_slide_image_indices(3, 4, [2, 2, 0, 1]) == [2, 2, 0, 1]

    def test_invalid_map_entries_fall_back_to_cycle(self):
        # None, out of range, negative, bool and non-int all fall back.
        assert _upload_slide_image_indices(3, 6, [None, 9, -1, True, "1", 1]) == [0, 1, 2, 0, 1, 1]

    def test_slide_count_clamped_to_two_and_ten(self):
        assert len(_upload_slide_image_indices(4, 25)) == 10
        assert len(_upload_slide_image_indices(4, 1)) == 2

    def test_bad_slide_count_falls_back_to_upload_count(self):
        assert _upload_slide_image_indices(3, "lots") == [0, 1, 2]

    def test_no_images_no_slides(self):
        assert _upload_slide_image_indices(0, 5) == []


class TestCalendarSlideOverride:
    def test_v2_request_accepts_num_slides(self):
        from app.agents.content_calendar_v2.models import CreateDraftRequestV2

        assert CreateDraftRequestV2(num_slides=8).num_slides == 8
        assert CreateDraftRequestV2().num_slides is None

    def test_v2_endpoint_clamps_user_choice_to_the_three_slide_business_rule(self):
        from app.agents.content_calendar_v2.routers import content_calendar_v2_router as mod

        source = inspect.getsource(mod.create_draft_from_item_v2)
        assert "planned_slides = max(2, min(3, int(request.num_slides)))" in source


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
