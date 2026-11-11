"""
TikTok ad groups also target Video interactions, Creator interactions and Hashtag interactions (and Purchase
intention where the objective allows it), not only Interests. All of it is best effort: it must never cost a launch.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.jane_ads.adapters import tiktok_behavior_targeting as bt
from app.agents.jane_ads.adapters.tiktok import _best_tiktok_interest
from tests.test_jane_ads_tiktok_adapter import _adapter, _auth, _plan, _run
from tests.test_jane_ads_tiktok_launch_fixes import _RoutedClient

ACTION_CATEGORIES = {"action_categories": [
    {"action_category_id": "30", "action_category_name": "Education", "sub_category_ids": []},
    {"action_category_id": "31", "action_category_name": "Food & Beverage", "sub_category_ids": []},
]}
HASHTAGS = {"hashtags": [{"hashtag_id": "h1", "hashtag_name": "learnontiktok"}, {"hashtag_id": "h2", "hashtag_name": "study"}]}


def _lookups(**by_path):
    async def fake(path, params, token):
        value = by_path.get(path)
        return value
    return patch.object(bt, "_lookup", side_effect=fake)


def _resolve(names, want_purchase_intent=False, **by_path):
    with _lookups(**by_path):
        return _run(bt.resolve_behavior_targeting(names, "adv1", "tok", _best_tiktok_interest, want_purchase_intent))


# ── reading TikTok's lookups ──────────────────────────────────────────────────

def test_entries_are_found_however_the_response_nests_them():
    nested = {"action_categories": [{"action_category_id": 1, "action_category_name": "A",
                                     "sub_categories": [{"id": "2", "name": "B"}]}]}
    got = bt._entries(nested, ("action_category_id", "id"), ("action_category_name", "name"))
    assert got == [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]
    assert bt._entries({}, ("id",), ("name",)) == []
    assert bt._entries(None, ("id",), ("name",)) == []


# ── what gets targeted ────────────────────────────────────────────────────────

def test_video_and_hashtag_audiences_are_built_for_the_plans_topics():
    got = _resolve(["Education"], **{"/tool/action_category/": ACTION_CATEGORIES, "/tool/hashtag/recommend/": HASHTAGS})
    scenes = {a["action_scene"]: a for a in got.actions}
    assert set(scenes) == {"VIDEO_RELATED", "HASHTAG_RELATED"}              # TikTok rejects creator ids, so none are sent
    assert scenes["VIDEO_RELATED"]["action_category_ids"] == ["30"]
    assert scenes["VIDEO_RELATED"]["video_user_actions"] == ["WATCHED_TO_END", "LIKED", "COMMENTED", "SHARED"]
    assert scenes["VIDEO_RELATED"]["action_period"] == 15
    assert scenes["HASHTAG_RELATED"] == {"action_scene": "HASHTAG_RELATED", "action_period": 0,
                                         "video_user_actions": ["VIEW_HASHTAG"], "action_category_ids": ["h1", "h2"]}


def test_a_topic_with_no_matching_category_adds_no_video_or_creator_audience():
    got = _resolve(["Quantum plumbing"], **{"/tool/action_category/": ACTION_CATEGORIES})
    assert got.actions == []


def test_no_topics_means_nothing_is_looked_up():
    with patch.object(bt, "_lookup", new=AsyncMock()) as lookup:
        got = _run(bt.resolve_behavior_targeting([], "adv1", "tok", _best_tiktok_interest, True))
    assert got.fields() == {} and lookup.await_count == 0


def test_failed_lookups_just_leave_the_audiences_out():
    got = _resolve(["Education"])                      # every lookup answers None
    assert got.fields() == {}


# ── purchase intention ────────────────────────────────────────────────────────

PURCHASE = {"keywords": [{"keyword_id": "p1", "keyword": "online courses"}, {"keyword_id": "p2", "keyword": "tutors"}]}


def test_purchase_intention_is_looked_up_only_when_asked_for():
    asked = _resolve(["Education"], True, **{"/tool/interest_keyword/recommend/": PURCHASE})
    assert asked.purchase_intention_keyword_ids == ["p1", "p2"]
    assert _resolve(["Education"], False, **{"/tool/interest_keyword/recommend/": PURCHASE}).purchase_intention_keyword_ids == []


def test_purchase_intention_goes_with_the_interactions_on_every_objective():
    t = bt.BehaviorTargeting(actions=[{"action_scene": "VIDEO_RELATED"}], purchase_intention_keyword_ids=["p1"])
    assert t.fields() == {"actions": [{"action_scene": "VIDEO_RELATED"}], "purchase_intention_keyword_ids": ["p1"]}


def test_attempts_go_from_everything_down_to_nothing():
    video, hashtag = {"action_scene": "VIDEO_RELATED"}, {"action_scene": "HASHTAG_RELATED"}
    t = bt.BehaviorTargeting(actions=[video, hashtag], purchase_intention_keyword_ids=["p1"])
    assert t.attempts() == [
        {"actions": [video, hashtag], "purchase_intention_keyword_ids": ["p1"]},   # everything
        {"actions": [video, hashtag]},                                              # without purchase intention
        {"actions": [video]}, {"actions": [hashtag]},                               # one interaction at a time
        {"purchase_intention_keyword_ids": ["p1"]},                                 # purchase intention alone
        {},                                                                         # nothing
    ]


def test_with_nothing_to_add_the_only_attempt_is_a_plain_ad_group():
    assert bt.BehaviorTargeting().attempts() == [{}]
    one = bt.BehaviorTargeting(actions=[{"action_scene": "VIDEO_RELATED"}])
    assert one.attempts() == [{"actions": [{"action_scene": "VIDEO_RELATED"}]}, {}]    # no duplicate attempts


# ── the launch ────────────────────────────────────────────────────────────────

class _PickyAdGroup(_RoutedClient):
    """Rejects any ad group create whose body `rejects(body)` says TikTok would not accept."""
    def __init__(self, rejects):
        super().__init__()
        self.rejects = rejects

    def _respond(self, method, url, kwargs):
        resp = super()._respond(method, url, kwargs)
        if url.endswith("/adgroup/create/") and self.rejects(kwargs.get("json") or {}):
            bad = {"code": 40002, "message": "Invalid behavior targeting category.", "data": {}}
            resp.json = lambda: bad
        return resp


PURCHASE_ANSWER = {"code": 0, "message": "OK", "data": {"recommend_keywords": [{"keyword_id": "p1", "keyword": "online courses"}]}}


def _launch_with_interests(client):
    plan = _plan(audience_targeting={"flexible_spec": [{"interests": [{"id": "m1", "name": "Education"}]}]})
    client.answers.update({
        "/tool/interest_category/": {"code": 0, "message": "OK", "data": {"interest_categories": [
            {"interest_category_id": "10", "interest_category_name": "Education", "level": 1}]}},
        "/tool/action_category/": {"code": 0, "message": "OK", "data": ACTION_CATEGORIES},
        "/tool/hashtag/recommend/": {"code": 0, "message": "OK", "data": HASHTAGS},
        "/tool/interest_keyword/recommend/": PURCHASE_ANSWER,
    })
    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = client
        return _run(_adapter().launch_campaign(plan, _auth()))


def test_the_launched_ad_group_carries_the_interactions_next_to_the_interests():
    client = _RoutedClient()
    result = _launch_with_interests(client)
    body = client.sent("/adgroup/create/")[0]
    assert result.launched is True
    assert body["interest_category_ids"] == ["10"]
    assert {a["action_scene"] for a in body["actions"]} == {"VIDEO_RELATED", "HASHTAG_RELATED"}
    assert body["purchase_intention_keyword_ids"] == ["p1"]


def test_if_tiktok_rejects_every_extra_audience_the_ad_group_is_made_without_them():
    client = _PickyAdGroup(lambda body: "actions" in body)
    result = _launch_with_interests(client)
    sent = client.sent("/adgroup/create/")
    assert result.launched is True
    assert "actions" in sent[0] and "actions" not in sent[-1]
    assert sent[-1]["interest_category_ids"] == ["10"]                       # the interests are kept


def test_one_scene_tiktok_does_not_accept_does_not_cost_the_others():
    # whole set rejected, and the hashtag scene is the bad one: the video scene alone is accepted
    client = _PickyAdGroup(lambda body: any(a["action_scene"] == "HASHTAG_RELATED" for a in body.get("actions", [])))
    result = _launch_with_interests(client)
    final = client.sent("/adgroup/create/")[-1]
    assert result.launched is True
    assert [a["action_scene"] for a in final["actions"]] == ["VIDEO_RELATED"]


def test_hashtag_keywords_are_plain_words_not_metas_qualified_names():
    assert bt._plain("Education (social concept)") == "Education"
    assert bt._plain("Early childhood education (primary and secondary education)") == "Early childhood education"
    assert bt._plain("Parents (All)") == "Parents"


def test_if_tiktok_does_not_accept_purchase_intention_the_interactions_are_kept():
    client = _PickyAdGroup(lambda body: "purchase_intention_keyword_ids" in body)
    result = _launch_with_interests(client)
    final = client.sent("/adgroup/create/")[-1]
    assert result.launched is True
    assert "purchase_intention_keyword_ids" not in final
    assert {a["action_scene"] for a in final["actions"]} == {"VIDEO_RELATED", "HASHTAG_RELATED"}
