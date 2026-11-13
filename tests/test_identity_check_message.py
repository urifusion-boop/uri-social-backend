"""
Facebook's identity checkpoint (Graph error 368 / subcode 4854002) is on the personal Facebook
account that manages the Page. Live on 2026-11-13 an owner kept disconnecting and reconnecting
because the message didn't make clear that wouldn't help. Every place a publish can fail now
shows one plain message, without Facebook's raw text repeated after it.
"""
from app.agents.social_media_manager.services import publish_confirmation as pc
from app.agents.social_media_manager.services.video_publish_service import VideoPublishService

RAW = ("Facebook publish failed: Confirm your identity before you can publish as this Page. "
       "Open the Facebook app on your phone and follow the instructions.")


def test_facebook_message_is_plain_and_says_reconnecting_wont_help():
    info = pc.explain("facebook", RAW)
    assert info["reconnect"] is False
    msg = info["message"]
    assert msg.startswith("Facebook is asking you to confirm your identity")
    assert "reconnecting won't fix it" in msg
    assert "Facebook app on your phone" in msg
    assert "publish this post again" in msg
    assert "Facebook said" not in msg and "Facebook publish failed" not in msg


def test_instagram_post_points_to_the_facebook_app_not_instagram():
    msg = pc.explain("instagram", RAW)["message"]
    assert "post to Instagram from this Page" in msg
    assert "Facebook app on your phone" in msg
    assert "Instagram app" not in msg


def test_specific_message_only_for_catalogued_causes():
    assert pc.specific_message("facebook", RAW).startswith("Facebook is asking you")
    assert pc.specific_message("facebook", "Some unrelated failure") is None
    # A generic token problem keeps its own handling elsewhere; not forced here.
    assert pc.specific_message("facebook", "Error validating access token") is None


def test_a_video_publish_job_shows_the_same_message():
    fields = VideoPublishService._failure_update(Exception(f"FB video post error: {RAW}"), "facebook")
    assert fields["status"] == "failed"
    assert fields["error"].startswith("Facebook is asking you to confirm your identity")


def test_a_video_job_with_an_unknown_error_keeps_its_own_text():
    fields = VideoPublishService._failure_update(Exception("something odd"), "facebook")
    assert fields["error"] == "something odd"
