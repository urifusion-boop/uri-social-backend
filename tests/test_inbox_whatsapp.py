"""WhatsApp in the unified inbox: webhook parsing, delivery statuses, connecting a
number and sending, against a fake Meta."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.inbox import whatsapp
from app.agents.inbox.entities import CHANNEL_ACCOUNTS, MESSAGES
from app.agents.inbox.ingest import parse_meta_event

mongomock_motor = pytest.importorskip("mongomock_motor")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def hook(messages=None, statuses=None, contacts=None):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "2348166462270", "phone_number_id": "PN1"},
            "contacts": contacts or [{"wa_id": "2348011112222", "profile": {"name": "Ada"}}],
            "messages": messages or [], "statuses": statuses or []}}]}]}


def test_a_text_message_becomes_an_inbox_dm():
    ev = parse_meta_event(hook([{"from": "2348011112222", "id": "wamid.1", "timestamp": "1760000000",
                                 "type": "text", "text": {"body": "Do you deliver to Ikeja?"}}]))
    assert len(ev) == 1
    e = ev[0]
    assert (e["type"], e["external_account_id"], e["external_thread_id"]) == ("dm", "PN1", "2348011112222")
    assert e["display_name"] == "Ada" and e["text"] == "Do you deliver to Ikeja?"
    assert e["provider_message_id"] == "wamid.1" and e["provider_timestamp"].year == 2025


def test_media_shows_a_label_and_keeps_the_caption():
    ev = parse_meta_event(hook([{"from": "2348011112222", "id": "wamid.2", "timestamp": "1760000000",
                                 "type": "image", "image": {"id": "MEDIA1", "mime_type": "image/jpeg",
                                                            "caption": "this one"}}]))
    assert ev[0]["text"] == "[Photo] this one"
    assert ev[0]["attachments"] == [{"type": "image", "media_id": "MEDIA1", "mime_type": "image/jpeg"}]


def test_no_profile_name_falls_back_to_the_number_and_ads_are_credited():
    ev = parse_meta_event(hook(
        [{"from": "2348033334444", "id": "wamid.3", "timestamp": "1760000000", "type": "text",
          "text": {"body": "hi"}, "referral": {"source_type": "ad", "source_id": "AD9"}}], contacts=[]))
    assert ev[0]["display_name"] == "+2348033334444" and ev[0]["source_ad_id"] == "AD9"


def test_statuses_are_not_messages_but_update_delivery():
    payload = hook(statuses=[{"id": "wamid.out", "status": "failed", "timestamp": "1760000000",
                              "errors": [{"title": "Re-engagement message"}]}])
    assert parse_meta_event(payload) == []
    db = mongomock_motor.AsyncMongoMockClient()["wa"]
    run(db[MESSAGES].insert_one({"provider_message_id": "wamid.out", "direction": "outbound"}))
    assert run(whatsapp.apply_statuses(db, payload)) == 1
    msg = run(db[MESSAGES].find_one({"provider_message_id": "wamid.out"}))
    assert msg["provider_status"] == "failed" and msg["failure_reason"] == "Re-engagement message"


def _client(responses):
    client = MagicMock()
    client.request = AsyncMock(side_effect=[MagicMock(json=MagicMock(return_value=r)) for r in responses])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client


def test_connecting_subscribes_registers_and_stores_the_channel():
    db = mongomock_motor.AsyncMongoMockClient()["wa"]
    cm, client = _client([{"success": True},
                          {"display_phone_number": "+234 816 646 2270", "verified_name": "URI", "quality_rating": "GREEN"},
                          {"success": True}])
    with patch("app.agents.inbox.whatsapp.httpx.AsyncClient", return_value=cm), \
         patch("app.agents.inbox.channels.prune_stale_claims", new=AsyncMock(return_value=0)):
        out = run(whatsapp.connect(db, "ws1", token="T", waba_id="WABA1", phone_number_id="PN1"))
    calls = [(c.args[0], c.args[1].rsplit("/", 2)[-2:]) for c in client.request.call_args_list]
    assert calls[0] == ("POST", ["WABA1", "subscribed_apps"]) and calls[2] == ("POST", ["PN1", "register"])
    row = run(db[CHANNEL_ACCOUNTS].find_one({"external_account_id": "PN1"}))
    assert row["platform"] == "whatsapp" and row["workspace_id"] == "ws1" and row["waba_id"] == "WABA1"
    assert out["display_phone_number"] == "+234 816 646 2270"


def test_coexistence_numbers_are_not_re_registered():
    db = mongomock_motor.AsyncMongoMockClient()["wa"]
    cm, client = _client([{"success": True}, {"display_phone_number": "+1", "verified_name": "X"}])
    with patch("app.agents.inbox.whatsapp.httpx.AsyncClient", return_value=cm), \
         patch("app.agents.inbox.channels.prune_stale_claims", new=AsyncMock(return_value=0)):
        run(whatsapp.connect(db, "ws1", token="T", waba_id="W", phone_number_id="P", coexistence=True))
    assert client.request.call_count == 2


def test_inbox_replies_on_a_whatsapp_thread_go_through_the_cloud_api():
    from app.agents.inbox.meta_transport import meta_transport
    with patch("app.agents.inbox.whatsapp.send_text", new=AsyncMock(return_value="wamid.sent")) as send:
        mid = run(meta_transport({"platform": "whatsapp", "kind": "dm", "external_thread_id": "2348011112222"},
                                 {"external_account_id": "PN1", "access_token": "T"}, "Yes, we do!"))
    assert mid == "wamid.sent"
    send.assert_awaited_once()
    assert send.await_args.args[1:] == ("2348011112222", "Yes, we do!")


def test_a_closed_window_says_to_use_a_template():
    resp = MagicMock(status_code=400, content=b"x",
                     json=MagicMock(return_value={"error": {"code": 131047, "message": "Re-engagement"}}))
    client = MagicMock(post=AsyncMock(return_value=resp))
    cm = MagicMock(__aenter__=AsyncMock(return_value=client), __aexit__=AsyncMock(return_value=False))
    with patch("app.agents.inbox.whatsapp.httpx.AsyncClient", return_value=cm):
        with pytest.raises(Exception, match="template"):
            run(whatsapp.send_text({"external_account_id": "PN1", "access_token": "T"}, "234", "hi"))
