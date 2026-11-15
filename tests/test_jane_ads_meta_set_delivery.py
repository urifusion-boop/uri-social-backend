"""Switching a campaign on or off (MetaAdPlatformAdapter.set_delivery) reaches every ad set and ad."""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.jane_ads.adapters.meta import MetaAdPlatformAdapter


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _mock_client(responses):
    client = AsyncMock()
    it = iter(responses)

    async def _next(*a, **kw):
        r = AsyncMock()
        r.json = lambda: next(it)
        return r
    client.post.side_effect = _next
    client.get.side_effect = _next
    return client


def test_switching_a_several_ad_campaign_on_reaches_every_ad_set_and_ad_but_a_paused_one():
    """Found testing on staging 2026-11-15: Play only switched on the first ad set and ad, so in
    a campaign testing several ads every other ad stayed paused and never ran."""
    from mongomock_motor import AsyncMongoMockClient
    db = AsyncMongoMockClient()["t"]
    _run(db["jane_ads_meta_campaigns"].insert_one({
        "campaign_id": "cmp_1", "adset_id": "as_1", "ad_id": "ad_1",
        "adset_ids": ["as_1", "as_2"], "ad_ids": ["ad_1", "ad_2", "ad_3"]}))
    adapter = MetaAdPlatformAdapter(db, ad_account_id="123", access_token="tok")
    # A losing test ad paused on its own, through the adapter.
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"success": True}])
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(adapter.pause_ad("cmp_1", "ad_3"))

    def touched(active):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = _mock_client([{"success": True}] * 10)
            MockClient.return_value.__aenter__.return_value = mock_client
            out = _run(adapter.set_delivery("cmp_1", active))
        return [c.args[0].rsplit("/", 1)[-1] for c in mock_client.post.call_args_list], out

    on, out = touched(True)
    assert on == ["cmp_1", "as_1", "as_2", "ad_1", "ad_2"]          # ad_3 stays paused
    assert out["status"] == "ACTIVE" and all(out["updated"].values())
    off, _ = touched(False)
    assert off == ["cmp_1", "as_1", "as_2", "ad_1", "ad_2", "ad_3"]  # pausing stops everything


def test_an_old_single_ad_record_still_switches_on():
    from mongomock_motor import AsyncMongoMockClient
    db = AsyncMongoMockClient()["t"]
    _run(db["jane_ads_meta_campaigns"].insert_one({"campaign_id": "cmp_9", "adset_id": "as_9", "ad_id": "ad_9"}))
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _mock_client([{"success": True}] * 3)
        MockClient.return_value.__aenter__.return_value = mock_client
        _run(MetaAdPlatformAdapter(db, ad_account_id="123", access_token="tok").set_delivery("cmp_9", True))
    assert [c.args[0].rsplit("/", 1)[-1] for c in mock_client.post.call_args_list] == ["cmp_9", "as_9", "ad_9"]
