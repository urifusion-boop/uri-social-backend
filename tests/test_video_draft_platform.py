"""
A saved video draft's `platform` is the network ("instagram"), not the video screen's target name
("instagram_reels"): the account connections, the Content Library's "is it connected" check and the
approval / scheduling path all look the network up by it, so an "instagram_reels" draft could not be posted
from the Library. `platforms` keeps the name the screen sent.
"""
import asyncio

import pytest

from app.agents.social_media_manager.routers import complete_social_manager as R


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeDrafts:
    def __init__(self):
        self.inserted = []

    async def insert_many(self, docs):
        self.inserted.extend(docs)


class FakeDb:
    def __init__(self):
        self.drafts = FakeDrafts()

    def __getitem__(self, name):
        assert name == "content_drafts"
        return self.drafts


def _save(platforms):
    db = FakeDb()
    request = R.SaveVideoDraftRequest(merged_video_url="https://cdn/v.mp4", caption="hi", platforms=platforms)
    _run(R.save_video_draft(request, db=db, token={"claims": {"userId": "u1"}}))
    return db.drafts.inserted


@pytest.mark.parametrize("target,network", [
    ("instagram_reels", "instagram"), ("facebook_reels", "facebook"),
    ("tiktok", "tiktok"), ("instagram", "instagram"), ("linkedin", "linkedin"),
])
def test_the_network_is_what_a_target_is_called_in_the_draft(target, network):
    assert R.video_draft_platform(target) == network


def test_a_video_saved_for_instagram_and_facebook_is_two_drafts_named_for_their_networks():
    docs = _save(["instagram_reels", "facebook_reels"])
    assert [d["platform"] for d in docs] == ["instagram", "facebook"]
    assert [d["platforms"] for d in docs] == [["instagram_reels"], ["facebook_reels"]]   # the screen's names kept
    assert all(d["media_type"] == "video" and d["post_type"] == "reel" and d["video_url"] for d in docs)


def test_a_draft_with_no_platform_chosen_still_defaults_to_instagram():
    (doc,) = _save([])
    assert doc["platform"] == "instagram" and doc["platforms"] == ["instagram"]
