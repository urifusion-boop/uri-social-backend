"""
Live-reported: a client who picked Instagram & Facebook BY NAME
(preferred_platform == "meta") but whose Meta ads connection was broken, with a
working Google Ads account, still got a silent Google plan back — "It's picking
Google automatically" no matter what platform they selected in the UI.

_build_campaign_plan (router.py) is a giant, deeply stateful function with no
practical way to invoke it directly in a unit test (same reasoning as the other
tests on it, see test_jane_ads_destination.py's test_destination_is_asked_before_
the_creative_is_built) — these assert against its own source instead.
"""
import inspect

from app.agents.jane_ads import router


def _source():
    return inspect.getsource(router._build_campaign_plan)


def test_an_explicit_meta_request_is_never_silently_swallowed_by_the_google_fallback():
    src = _source()
    # The except AdsConnectionRequired block must surface the real connection
    # problem (not silently fall through into the google_only substitution)
    # when the client explicitly asked for Meta — not just when Google is
    # unavailable too.
    except_block_start = src.index("except AdsConnectionRequired as e:")
    google_only_assignment = src.index("google_only = ads_conn is None")
    assert except_block_start < google_only_assignment

    except_block = src[except_block_start:google_only_assignment]
    assert 'body.preferred_platform == "meta"' in except_block
    assert 'not google_ready or body.preferred_platform == "meta"' in except_block


def test_the_meta_connection_early_return_still_fires_when_google_is_not_ready_either():
    # The original behaviour (no Google account at all) must still work —
    # this fix only ADDS a second condition, it doesn't narrow the first one.
    src = _source()
    except_block_start = src.index("except AdsConnectionRequired as e:")
    google_only_assignment = src.index("google_only = ads_conn is None")
    except_block = src[except_block_start:google_only_assignment]
    assert "not google_ready" in except_block
    assert 'stage": f"meta_connection_{e.state.value}"' in except_block
