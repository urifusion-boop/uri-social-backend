import asyncio
from types import SimpleNamespace

from app.agents.jane_ads import existing_posts as ep
from app.agents.jane_ads.adapters.meta import MetaAdPlatformAdapter
from app.agents.jane_ads.creative import creative_from_existing_post


def test_instagram_video_uses_its_thumbnail():
    p = ep.instagram_post({"id": "1", "media_type": "VIDEO", "media_url": "v.mp4",
                           "thumbnail_url": "t.jpg", "caption": "hi"})
    assert p["image_url"] == "t.jpg" and p["is_video"] and p["platform"] == "instagram"


def test_facebook_post_shape():
    p = ep.facebook_post({"id": "9_1", "message": "m", "full_picture": "p.jpg",
                          "attachments": {"data": [{"media_type": "photo"}]}})
    assert (p["post_id"], p["text"], p["image_url"], p["is_video"]) == ("9_1", "m", "p.jpg", False)


def test_creative_carries_the_post_reference():
    ad = creative_from_existing_post("instagram", "17", "t.jpg", "caption")
    assert ad.existing_post == {"platform": "instagram", "post_id": "17"}
    assert ad.image_url == "t.jpg" and ad.primary_text == "caption"


def _adapter(ig):
    a = MetaAdPlatformAdapter.__new__(MetaAdPlatformAdapter)

    async def ids(_page):
        return ig
    a._instagram_ids = ids
    return a


PLAN = SimpleNamespace(business_id="b", page_id="55")


def test_instagram_post_ad_references_the_media_and_account():
    body = asyncio.run(_adapter({"id": "ig1"})._existing_post_creative_body(
        PLAN, {"platform": "instagram", "post_id": "17"}, ""))
    assert body["source_instagram_media_id"] == "17" and body["instagram_user_id"] == "ig1"


def test_instagram_post_without_a_linked_account_is_refused():
    try:
        asyncio.run(_adapter({})._existing_post_creative_body(
            PLAN, {"platform": "instagram", "post_id": "17"}, ""))
    except ValueError as e:
        assert "linked Instagram" in str(e)
    else:
        raise AssertionError("expected a refusal")


def test_facebook_post_ad_uses_the_story_id():
    body = asyncio.run(_adapter({})._existing_post_creative_body(
        PLAN, {"platform": "facebook", "post_id": "77"}, ""))
    assert body["object_story_id"] == "55_77"
