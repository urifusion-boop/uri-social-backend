"""The price of a generated video: the launch matrix, the in-between lengths, and the guards."""
import pytest

from app.agents.social_media_manager.services import video_pricing as P


MATRIX = {
    "quick_video":         [2, 3, 5, 9],
    "animate_product":     [2, 3, 5, 9],
    "talking_nigerian":    [2, 3, 6, 11],
    "social_video":        [2, 3, 6, 12],
    "high_quality":        [2, 4, 8, 15],
    "complex_cinematic":   [2, 5, 10, 19],
    "talking_standard":    [3, 9, 18, 35],
    "advanced_references": [5, 14, 26, 52],
}


@pytest.mark.parametrize("video_type,expected", sorted(MATRIX.items()))
def test_every_cell_of_the_approved_matrix_is_charged_exactly(video_type, expected):
    assert [P.credits_for(video_type, s) for s in (5, 15, 30, 60)] == expected


@pytest.mark.parametrize("video_type", sorted(MATRIX))
def test_a_longer_video_never_costs_less_than_a_shorter_one(video_type):
    prices = [P.credits_for(video_type, s) for s in range(1, 61)]
    assert prices == sorted(prices)
    assert prices[0] == MATRIX[video_type][0]        # under 5 seconds still costs the 5-second price


@pytest.mark.parametrize("seconds,expected", [(10, 3), (20, 4), (40, 7), (50, 8)])
def test_the_lengths_the_screen_offers_but_the_table_skips_are_rounded_up(seconds, expected):
    assert P.credits_for("quick_video", seconds) == expected


def test_a_video_over_a_minute_cannot_be_priced():
    with pytest.raises(P.PricingError):
        P.credits_for("quick_video", 61)
    with pytest.raises(P.PricingError):
        P.credits_for("quick_video", 0)


def test_the_price_comes_from_the_scenes_not_from_what_the_client_says():
    tampered = {"scenes": [{"duration_seconds": 600}]}
    q = P.quote_for(tampered, "quick_video")
    assert q["seconds"] == 15                        # the longest one clip can run
    assert P.billable_seconds([{"duration_seconds": "junk"}, {}, None]) == 15   # unreadable = default 5s each


def test_too_many_scenes_or_none_is_refused():
    with pytest.raises(P.PricingError):
        P.quote_for({"scenes": [{"duration_seconds": 5}] * 13}, "quick_video")
    with pytest.raises(P.PricingError):
        P.quote_for({"scenes": []}, "quick_video")


def test_a_fifteen_second_quick_video_costs_three_credits():
    q = P.quote_for({"scenes": [{"duration_seconds": 5}] * 3}, "quick_video")
    assert q["credits_required"] == 3 and q["video_type"] == "quick_video" and q["seconds"] == 15


@pytest.mark.parametrize("outcome,voice,expected", [
    ("talking_dialogue", "Femi (Nigerian)", "talking_nigerian"),
    ("talking_dialogue", "Idera (Nigerian)", "talking_nigerian"),
    ("talking_dialogue", "Sarah", "talking_standard"),
    ("quick_video", "Femi (Nigerian)", "quick_video"),     # the voice only matters for talking videos
    ("something_new", "Sarah", "quick_video"),              # unknown = what the generator itself falls back to
])
def test_the_video_type_follows_the_outcome_and_voice(outcome, voice, expected):
    assert P.video_type_for(outcome, voice) == expected


@pytest.mark.parametrize("charged,total,delivered,expected", [
    (3, 3, 3, 0), (3, 3, 2, 1), (3, 3, 1, 2), (3, 3, 0, 3),
    (9, 12, 11, 1), (9, 12, 6, 5), (9, 12, 0, 9), (1, 3, 2, 1),
    (0, 3, 0, 0), (5, 0, 0, 0),
])
def test_scenes_that_never_came_out_are_refunded_in_the_customers_favour(charged, total, delivered, expected):
    assert P.refund_for_failed_scenes(charged, total, delivered) == expected


def test_a_refund_never_exceeds_the_charge():
    for charged in range(1, 20):
        for total in range(1, 13):
            for delivered in range(0, total + 1):
                assert 0 <= P.refund_for_failed_scenes(charged, total, delivered) <= charged


def test_the_naira_reference_is_800_a_credit():
    assert P.CREDIT_NAIRA_REFERENCE == 800
