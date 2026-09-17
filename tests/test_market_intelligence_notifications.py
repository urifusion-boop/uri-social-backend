"""
Uri Market Intelligence — notification categorization, outbox and delivery
tests (PRD §14, P0-11).
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.market_intelligence import notification_delivery
from app.agents.market_intelligence.models import (
    ConfidenceBand,
    EvidenceType,
    InsightStatus,
    InsightVersion,
    Lifecycle,
    NotificationCategory,
    OutboxDeliveryMode,
    OutboxStatus,
    ScoreBreakdown,
    Topic,
    SourceConfig,
    UrgencyAssessment,
)
from app.agents.market_intelligence.notifications import categorize_insight, queue_notification


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _score(total: int) -> ScoreBreakdown:
    return ScoreBreakdown(components=[], total=total, band=ScoreBreakdown.band_for(total))


def _insight(**overrides) -> InsightVersion:
    base = dict(
        id="i1", revision=1, brand_id="b1", topic_id="t1", cluster_id="c1",
        type=EvidenceType.CUSTOMER_CONCERN, headline="h", observed_change="oc",
        business_implication="bi", suggested_next_step="sns", evidence_ids=["e1"],
        confidence=_score(3), relevance=_score(3),
        urgency=UrgencyAssessment(is_urgent=False, reason="n/a"),
        lifecycle=Lifecycle.UNKNOWN, status=InsightStatus.ACTIVE,
        first_seen=datetime.utcnow(), last_updated=datetime.utcnow(),
    )
    base.update(overrides)
    return InsightVersion(**base)


# ── categorize_insight ───────────────────────────────────────────────────────

def test_revision_bump_is_always_material_update():
    insight = _insight(revision=2, type=EvidenceType.CUSTOMER_CONCERN, confidence=_score(1), relevance=_score(1))
    assert categorize_insight(insight) == NotificationCategory.MATERIAL_UPDATE


def test_purchase_inquiry_is_qualified_inquiry():
    insight = _insight(type=EvidenceType.PURCHASE_INQUIRY)
    assert categorize_insight(insight) == NotificationCategory.QUALIFIED_INQUIRY


def test_upcoming_development_is_prepare():
    insight = _insight(type=EvidenceType.UPCOMING_DEVELOPMENT)
    assert categorize_insight(insight) == NotificationCategory.PREPARE


def test_cooling_lifecycle_is_cooling_category():
    insight = _insight(type=EvidenceType.EMERGING_TREND, lifecycle=Lifecycle.COOLING)
    assert categorize_insight(insight) == NotificationCategory.COOLING


def test_reputation_risk_never_queues_a_notification_even_when_urgent():
    # Even an urgent, high-confidence, high-relevance reputation_risk finding
    # must never auto-notify — PRD §14 requires human review first.
    insight = _insight(
        type=EvidenceType.REPUTATION_RISK,
        urgency=UrgencyAssessment(is_urgent=True, reason="looks severe"),
        confidence=_score(9), relevance=_score(9),
    )
    assert categorize_insight(insight) is None


def test_reputation_risk_revision_bump_still_never_queues():
    # A revision bump normally forces MATERIAL_UPDATE — reputation_risk
    # overrides even that.
    insight = _insight(type=EvidenceType.REPUTATION_RISK, revision=3)
    assert categorize_insight(insight) is None


def test_urgent_high_confidence_high_relevance_is_act_soon():
    insight = _insight(
        urgency=UrgencyAssessment(is_urgent=True, reason="deadline soon"),
        confidence=_score(9), relevance=_score(9),
    )
    assert categorize_insight(insight) == NotificationCategory.ACT_SOON


def test_medium_confidence_and_relevance_is_useful_pattern():
    insight = _insight(confidence=_score(6), relevance=_score(6))
    assert categorize_insight(insight) == NotificationCategory.USEFUL_PATTERN


def test_low_confidence_or_relevance_is_early_signal_none():
    insight = _insight(confidence=_score(2), relevance=_score(2))
    assert categorize_insight(insight) is None


# ── queue_notification ───────────────────────────────────────────────────────

class FakeOutboxCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeOutboxDb:
    def __init__(self):
        self._coll = FakeOutboxCollection()

    def __getitem__(self, name):
        return self._coll


def _topic() -> Topic:
    return Topic(
        id="t1", brand_id="b1", user_id="u1", question="q", keywords=["x"],
        sources=[SourceConfig(provider="mock", platform="mock")],
    )


def test_queue_notification_creates_entry_with_correct_delivery_mode():
    db = FakeOutboxDb()
    insight = _insight(type=EvidenceType.PURCHASE_INQUIRY)
    entry = _run(queue_notification(db, insight, _topic()))
    assert entry is not None
    assert entry.category == NotificationCategory.QUALIFIED_INQUIRY
    assert entry.delivery_mode == OutboxDeliveryMode.IMMEDIATE
    assert len(db["mi_outbox"].docs) == 1


def test_queue_notification_returns_none_for_early_signal():
    db = FakeOutboxDb()
    insight = _insight(confidence=_score(1), relevance=_score(1))
    entry = _run(queue_notification(db, insight, _topic()))
    assert entry is None
    assert db["mi_outbox"].docs == []


def test_queue_notification_is_deduped_by_dedupe_key():
    db = FakeOutboxDb()
    insight = _insight(type=EvidenceType.PURCHASE_INQUIRY)
    first = _run(queue_notification(db, insight, _topic()))
    second = _run(queue_notification(db, insight, _topic()))
    assert first is not None
    assert second is None
    assert len(db["mi_outbox"].docs) == 1


# ── process_outbox / send_daily_digests ─────────────────────────────────────

class GenericFakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


class GenericFakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    def _matches(self, doc, query):
        for key, cond in query.items():
            val = doc.get(key)
            if isinstance(cond, dict) and "$gte" in cond:
                if val is None or val < cond["$gte"]:
                    return False
            elif isinstance(cond, dict) and "$in" in cond:
                if val not in cond["$in"]:
                    return False
            elif val != cond:
                return False
        return True

    def find(self, query=None, projection=None):
        return GenericFakeCursor([dict(d) for d in self.docs if self._matches(d, query or {})])

    async def find_one(self, query=None, sort=None):
        matches = [d for d in self.docs if self._matches(d, query or {})]
        return dict(matches[0]) if matches else None

    async def count_documents(self, query):
        return len([d for d in self.docs if self._matches(d, query)])

    async def insert_one(self, doc):
        if "_id" in doc and any(d.get("_id") == doc["_id"] for d in self.docs):
            from pymongo.errors import DuplicateKeyError
            raise DuplicateKeyError(f"duplicate _id: {doc['_id']}")
        self.docs.append(dict(doc))

    async def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class GenericFakeDb:
    def __init__(self):
        self._colls: dict[str, GenericFakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, GenericFakeCollection())


def _seed_insight(db, insight_id="i1", status="active", is_urgent=False):
    db["mi_insights"].docs.append({
        "id": insight_id, "status": status, "headline": "h", "observed_change": "oc",
        "suggested_next_step": "sns", "urgency": {"is_urgent": is_urgent},
    })


def _seed_outbox(db, category, delivery_mode="immediate", topic_id="t1", user_id="u1", entry_id="o1", status="queued"):
    entry = {
        "id": entry_id, "brand_id": "b1", "user_id": user_id, "topic_id": topic_id,
        "insight_id": "i1", "insight_revision": 1, "category": category, "delivery_mode": delivery_mode,
        "dedupe_key": f"key-{entry_id}", "status": status,
    }
    db["mi_outbox"].docs.append(entry)
    return entry


def _seed_user(db, user_id="u1", email="user@example.com"):
    db["users"].docs.append({"userId": user_id, "email": email})


def _enable_email(db, user_id="u1", brand_id="b1", **overrides):
    prefs = {"user_id": user_id, "brand_id": brand_id, "email_enabled": True, "timezone": "UTC",
             "digest_hour_local": 8, "quiet_hours_start_local": 21, "quiet_hours_end_local": 8,
             "urgent_override": False, "muted_topic_ids": [], "muted_categories": [], "snoozed_insight_ids": []}
    prefs.update(overrides)
    db["mi_preferences"].docs.append(prefs)
    return prefs


def test_process_outbox_sends_when_everything_is_eligible():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db)
    _seed_outbox(db, NotificationCategory.ACT_SOON.value)

    with patch.object(notification_delivery.email_service, "send_raw_email", new=AsyncMock(return_value=True)):
        result = _run(notification_delivery.process_outbox(db, now=datetime(2024, 6, 1, 12, 0)))  # noon UTC, outside quiet hours

    assert result == {"sent": 1, "suppressed": 0, "failed": 0}
    assert db["mi_outbox"].docs[0]["status"] == "sent"


def test_process_outbox_suppresses_when_email_not_enabled():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    # No preferences seeded -> get_or_create_preferences creates a default (email_enabled=False)
    _seed_outbox(db, NotificationCategory.ACT_SOON.value)

    result = _run(notification_delivery.process_outbox(db, now=datetime(2024, 6, 1, 12, 0)))
    assert result == {"sent": 0, "suppressed": 1, "failed": 0}
    assert db["mi_outbox"].docs[0]["suppression_reason"] == "email not opted in — in-app only"


def test_process_outbox_suppresses_expired_qualified_inquiry():
    db = GenericFakeDb()
    _seed_insight(db, is_urgent=False)  # no longer urgent -> expired
    _seed_user(db)
    _enable_email(db)
    _seed_outbox(db, NotificationCategory.QUALIFIED_INQUIRY.value)

    result = _run(notification_delivery.process_outbox(db, now=datetime(2024, 6, 1, 12, 0)))
    assert result["suppressed"] == 1
    assert "expired" in db["mi_outbox"].docs[0]["suppression_reason"]


def test_process_outbox_suppresses_muted_topic():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db, muted_topic_ids=["t1"])
    _seed_outbox(db, NotificationCategory.ACT_SOON.value)

    result = _run(notification_delivery.process_outbox(db, now=datetime(2024, 6, 1, 12, 0)))
    assert result["suppressed"] == 1
    assert db["mi_outbox"].docs[0]["suppression_reason"] == "topic muted"


def test_process_outbox_suppresses_snoozed_insight():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db, snoozed_insight_ids=["i1"])
    _seed_outbox(db, NotificationCategory.ACT_SOON.value)

    result = _run(notification_delivery.process_outbox(db, now=datetime(2024, 6, 1, 12, 0)))
    assert result["suppressed"] == 1
    assert db["mi_outbox"].docs[0]["suppression_reason"] == "insight snoozed"


def test_process_outbox_respects_topic_cooldown():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db)
    now = datetime(2024, 6, 1, 12, 0)
    # A prior SENT entry for the same topic within the last 6 hours.
    db["mi_outbox"].docs.append({
        "id": "prior", "brand_id": "b1", "user_id": "u1", "topic_id": "t1",
        "status": "sent", "sent_at": now - timedelta(hours=1), "delivery_mode": "immediate",
        "category": NotificationCategory.USEFUL_PATTERN.value,
    })
    _seed_outbox(db, NotificationCategory.ACT_SOON.value, entry_id="o2")

    result = _run(notification_delivery.process_outbox(db, now=now))
    entry = next(d for d in db["mi_outbox"].docs if d["id"] == "o2")
    assert entry["status"] == "suppressed"
    assert entry["suppression_reason"] == "topic cooldown active"


def test_material_update_bypasses_topic_cooldown_but_not_quiet_hours():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db)
    now = datetime(2024, 6, 1, 22, 0)  # 22:00 UTC — inside default quiet hours (21-08)
    db["mi_outbox"].docs.append({
        "id": "prior", "brand_id": "b1", "user_id": "u1", "topic_id": "t1",
        "status": "sent", "sent_at": now - timedelta(hours=1), "delivery_mode": "immediate",
        "category": NotificationCategory.USEFUL_PATTERN.value,
    })
    _seed_outbox(db, NotificationCategory.MATERIAL_UPDATE.value, entry_id="o2")

    with patch.object(notification_delivery.email_service, "send_raw_email", new=AsyncMock(return_value=True)):
        result = _run(notification_delivery.process_outbox(db, now=now))

    entry = next(d for d in db["mi_outbox"].docs if d["id"] == "o2")
    # Cooldown bypassed (material update), but quiet hours still block it —
    # PRD: "quiet hours still apply unless the user has enabled an urgent override."
    assert entry["status"] == "suppressed"
    assert entry["suppression_reason"] == "quiet hours"


def test_material_update_with_urgent_override_bypasses_quiet_hours_too():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db, urgent_override=True)
    now = datetime(2024, 6, 1, 22, 0)
    _seed_outbox(db, NotificationCategory.MATERIAL_UPDATE.value)

    with patch.object(notification_delivery.email_service, "send_raw_email", new=AsyncMock(return_value=True)):
        result = _run(notification_delivery.process_outbox(db, now=now))

    assert result == {"sent": 1, "suppressed": 0, "failed": 0}


def test_process_outbox_respects_quiet_hours_for_ordinary_category():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db)
    now = datetime(2024, 6, 1, 3, 0)  # 03:00 UTC — inside default quiet hours (21-08, wraps midnight)
    _seed_outbox(db, NotificationCategory.ACT_SOON.value)

    result = _run(notification_delivery.process_outbox(db, now=now))
    assert result["suppressed"] == 1
    assert db["mi_outbox"].docs[0]["suppression_reason"] == "quiet hours"


def test_process_outbox_enforces_daily_cap():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db)
    now = datetime(2024, 6, 1, 12, 0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(3):
        db["mi_outbox"].docs.append({
            "id": f"prior{i}", "brand_id": "b1", "user_id": "u1", "topic_id": f"other_topic_{i}",
            "status": "sent", "sent_at": today_start + timedelta(hours=i), "delivery_mode": "immediate",
            "category": NotificationCategory.USEFUL_PATTERN.value,
        })
    _seed_outbox(db, NotificationCategory.ACT_SOON.value, topic_id="yet_another_topic")

    result = _run(notification_delivery.process_outbox(db, now=now))
    assert result["suppressed"] == 1
    assert "daily immediate-email cap" in db["mi_outbox"].docs[-1]["suppression_reason"]
    # Bundled into the digest instead of lost entirely.
    assert db["mi_outbox"].docs[-1]["delivery_mode"] == "digest"


def test_process_outbox_fails_gracefully_when_no_email_on_file():
    db = GenericFakeDb()
    _seed_insight(db)
    # No user seeded -> no email on file
    _enable_email(db)
    _seed_outbox(db, NotificationCategory.ACT_SOON.value)

    result = _run(notification_delivery.process_outbox(db, now=datetime(2024, 6, 1, 12, 0)))
    assert result == {"sent": 0, "suppressed": 0, "failed": 1}


def test_send_daily_digests_skips_when_not_due():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db, digest_hour_local=8)
    _seed_outbox(db, NotificationCategory.USEFUL_PATTERN.value, delivery_mode="digest")

    result = _run(notification_delivery.send_daily_digests(db, now=datetime(2024, 6, 1, 14, 0)))  # not 8am
    assert result["skipped_not_due"] == 1
    assert result["digests_sent"] == 0


def test_send_daily_digests_skips_filler_when_nothing_eligible():
    db = GenericFakeDb()
    _seed_insight(db, status="dismissed")  # no longer active
    _seed_user(db)
    _enable_email(db, digest_hour_local=8)
    _seed_outbox(db, NotificationCategory.USEFUL_PATTERN.value, delivery_mode="digest")

    result = _run(notification_delivery.send_daily_digests(db, now=datetime(2024, 6, 1, 8, 0)))
    assert result["skipped_empty"] == 1
    assert result["digests_sent"] == 0


def test_send_daily_digests_bundles_eligible_entries_into_one_email():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db, digest_hour_local=8)
    _seed_outbox(db, NotificationCategory.USEFUL_PATTERN.value, delivery_mode="digest", entry_id="o1")
    _seed_outbox(db, NotificationCategory.COOLING.value, delivery_mode="digest", entry_id="o2")

    with patch.object(notification_delivery.email_service, "send_raw_email", new=AsyncMock(return_value=True)) as mock_send:
        result = _run(notification_delivery.send_daily_digests(db, now=datetime(2024, 6, 1, 8, 0)))

    assert result["digests_sent"] == 1
    mock_send.assert_awaited_once()  # ONE email, not two
    assert all(d["status"] == "sent" for d in db["mi_outbox"].docs)


def test_send_daily_digests_claim_prevents_double_send_same_day():
    db = GenericFakeDb()
    _seed_insight(db)
    _seed_user(db)
    _enable_email(db, digest_hour_local=8)
    _seed_outbox(db, NotificationCategory.USEFUL_PATTERN.value, delivery_mode="digest")

    with patch.object(notification_delivery.email_service, "send_raw_email", new=AsyncMock(return_value=True)) as mock_send:
        _run(notification_delivery.send_daily_digests(db, now=datetime(2024, 6, 1, 8, 0)))
        # Simulate a second worker's tick at the same hour.
        _seed_outbox(db, NotificationCategory.PREPARE.value, entry_id="o2", delivery_mode="digest")
        _run(notification_delivery.send_daily_digests(db, now=datetime(2024, 6, 1, 8, 5)))

    mock_send.assert_awaited_once()  # second tick's claim fails -> no second email today


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
