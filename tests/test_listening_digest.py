"""
Weekly listening digest (PRD §10): bundles a business's top live cards from
the last 7 days into one notification, once per ISO week, and sends nothing
when there's nothing new. Uses an in-memory MongoDB; brand_notifier.notify
is stubbed so no real channel/user lookups happen.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import digest
from app.agents.market_intelligence.listening.config import DEFAULTS, _merge

NOW = datetime(2026, 10, 21, 9, 10)
CFG = _merge(DEFAULTS, {})


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["digest_test"]


async def _profile(db, brand_id="brand-1", status="active"):
    doc = {"brand_id": brand_id, "user_id": "user-1", "status": status}
    await db["mi_listening_profiles"].insert_one(dict(doc))
    return doc


async def _card(db, brand_id="brand-1", topic="latasha wig", score=70, status="shown",
                created_at=None, expires_at=None, type_="post"):
    cid = f"{brand_id}-{topic}-{score}"
    doc = {"id": cid, "brand_id": brand_id, "topic": topic, "topic_name": topic, "type": type_,
           "status": status, "scores": {"total": score},
           "created_at": created_at or NOW - timedelta(days=1),
           "expires_at": expires_at or NOW + timedelta(days=3)}
    await db["mi_listen_cards"].insert_one(dict(doc))
    return doc


def _notify_spy(monkeypatch, calls):
    async def fake_notify(db, user_id, brand_id, event, **kwargs):
        calls.append({"user_id": user_id, "brand_id": brand_id, "event": event, **kwargs})
        return {"sent": True}
    from app.services import brand_notifier
    monkeypatch.setattr(brand_notifier, "notify", fake_notify)


def test_sends_digest_with_top_cards_and_claims_the_week(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db))
    run(_card(db, score=90))
    run(_card(db, topic="frontal install", score=60))

    result = run(digest.send_weekly_digests(db, CFG, NOW))

    assert result == {"sent": 1, "skipped_empty": 0, "skipped_claimed": 0, "failed": 0}
    assert len(calls) == 1
    assert calls[0]["event"] == "trending_topic"
    assert calls[0]["metadata"]["mode"] == "weekly_digest"
    assert len(calls[0]["metadata"]["card_ids"]) == 2
    assert "latasha wig" in calls[0]["title"]  # highest-scoring card named first


def test_no_cards_skips_without_sending(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db))

    result = run(digest.send_weekly_digests(db, CFG, NOW))

    assert result["sent"] == 0 and result["skipped_empty"] == 1
    assert calls == []


def test_already_sent_this_week_is_not_sent_again(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db))
    run(_card(db))

    first = run(digest.send_weekly_digests(db, CFG, NOW))
    second = run(digest.send_weekly_digests(db, CFG, NOW + timedelta(hours=2)))  # same ISO week

    assert first["sent"] == 1
    assert second == {"sent": 0, "skipped_empty": 0, "skipped_claimed": 1, "failed": 0}
    assert len(calls) == 1


def test_next_week_sends_again(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db))
    run(_card(db, created_at=NOW - timedelta(days=1)))

    run(digest.send_weekly_digests(db, CFG, NOW))
    run(_card(db, topic="new topic", created_at=NOW + timedelta(days=7), expires_at=NOW + timedelta(days=14)))
    second = run(digest.send_weekly_digests(db, CFG, NOW + timedelta(days=8)))

    assert second["sent"] == 1
    assert len(calls) == 2


def test_respects_digest_max_items_cap_and_picks_highest_scoring(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db))
    for i, score in enumerate([40, 95, 60, 70, 50, 80, 30]):  # 7 cards, cap default is 5
        run(_card(db, topic=f"topic{i}", score=score))

    run(digest.send_weekly_digests(db, CFG, NOW))

    ids = calls[0]["metadata"]["card_ids"]
    assert len(ids) == CFG["delivery"]["digest_max_items"] == 5
    assert "brand-1-topic1-95" in ids  # the top-scoring card made the cut
    assert "brand-1-topic6-30" not in ids  # the lowest did not


def test_paused_profile_is_never_considered(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db, status="paused"))
    run(_card(db))

    result = run(digest.send_weekly_digests(db, CFG, NOW))

    assert result == {"sent": 0, "skipped_empty": 0, "skipped_claimed": 0, "failed": 0}
    assert calls == []


def test_dismissed_and_expired_cards_are_excluded(monkeypatch):
    db = fresh_db()
    calls = []
    _notify_spy(monkeypatch, calls)
    run(_profile(db))
    run(_card(db, topic="dismissed one", status="dismissed"))
    run(_card(db, topic="expired one", expires_at=NOW - timedelta(hours=1)))
    run(_card(db, topic="old one", created_at=NOW - timedelta(days=10)))  # outside the 7-day window

    result = run(digest.send_weekly_digests(db, CFG, NOW))

    assert result["sent"] == 0 and result["skipped_empty"] == 1
    assert calls == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
