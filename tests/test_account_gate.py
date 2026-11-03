"""
The per-account publish gate (services/rate_limit.py).

One post at a time per connected account, a minimum gap between posts, and a pause after a
limit. The real thing runs in Mongo across several workers; these tests use an in-memory stand-in
that genuinely evaluates the Mongo filter and models upsert-on-existing-_id as DuplicateKeyError,
so the query itself is exercised, not just the surrounding logic. (True cross-worker atomicity is
Mongo's job; the optional live check at the bottom runs only when MONGO_GATE_TEST_URI is set.)
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from app.agents.social_media_manager.services import rate_limit as rl

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
KEY = "tiktok:tt_1"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── An in-memory Mongo stand-in ───────────────────────────────────────────────

def _cmp(value, op, operand):
    if op == "$exists":
        return bool(operand) == (value is not _MISSING)
    if value is _MISSING:
        return False
    if op == "$lte":
        return value <= operand
    if op == "$in":
        return value in operand
    raise NotImplementedError(op)


_MISSING = object()


def _match(doc, filt):
    for field, cond in filt.items():
        if field == "$and":
            if not all(_match(doc, f) for f in cond):
                return False
        elif field == "$or":
            if not any(_match(doc, f) for f in cond):
                return False
        else:
            value = doc.get(field, _MISSING)
            if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
                if not all(_cmp(value, op, operand) for op, operand in cond.items()):
                    return False
            elif (None if value is _MISSING else value) != cond and not (cond is None and value is _MISSING):
                return False
    return True


def _apply(doc, update):
    for k, v in update.get("$set", {}).items():
        doc[k] = v
    for k, v in update.get("$inc", {}).items():
        doc[k] = doc.get(k, 0) + v
    for k in update.get("$unset", {}):
        doc.pop(k, None)


class FakeGateCollection:
    def __init__(self):
        self.docs = {}

    async def find_one(self, filt, projection=None):
        return next((dict(d) for d in self.docs.values() if _match(d, filt)), None)

    async def find_one_and_update(self, filt, update, upsert=False, return_document=None):
        doc = next((d for d in self.docs.values() if _match(d, filt)), None)
        if doc is None:
            if not upsert:
                return None
            if filt.get("_id") in self.docs:
                raise DuplicateKeyError("duplicate _id")
            doc = {"_id": filt["_id"]}
            self.docs[doc["_id"]] = doc
        _apply(doc, update)
        return dict(doc)

    async def update_one(self, filt, update, upsert=False):
        doc = next((d for d in self.docs.values() if _match(d, filt)), None)
        if doc is None:
            return None
        _apply(doc, update)


class FakeDb:
    def __init__(self):
        self.gate = FakeGateCollection()

    def __getitem__(self, name):
        assert name == rl.GATE_COLLECTION
        return self.gate


# ── evaluate_gate (pure) ──────────────────────────────────────────────────────

def test_no_document_is_free():
    assert rl.evaluate_gate(None, NOW).granted


def test_a_live_lease_by_someone_else_is_busy_until_it_expires():
    doc = {"lease_owner": "a", "lease_expires_at": NOW + timedelta(seconds=30)}
    d = rl.evaluate_gate(doc, NOW, owner="b")
    assert (d.granted, d.reason, d.retry_at) == (False, "busy", NOW + timedelta(seconds=30))
    assert rl.evaluate_gate(doc, NOW + timedelta(seconds=31), owner="b").granted


def test_the_owner_may_hold_its_own_lease():
    assert rl.evaluate_gate({"lease_owner": "a", "lease_expires_at": NOW + timedelta(seconds=30)}, NOW, owner="a").granted


def test_the_gap_after_the_last_post_is_respected():
    d = rl.evaluate_gate({"next_allowed_at": NOW + timedelta(seconds=12)}, NOW)
    assert (d.granted, d.reason) == (False, "spaced") and d.retry_at == NOW + timedelta(seconds=12)


def test_a_pause_beats_everything_else():
    doc = {"paused_until": NOW + timedelta(minutes=5), "pause_code": "17", "pause_reason": "limit",
           "lease_expires_at": NOW + timedelta(seconds=5), "lease_owner": "a"}
    d = rl.evaluate_gate(doc, NOW, owner="a")
    assert (d.granted, d.reason, d.pause_code) == (False, "paused", "17")


def test_naive_datetimes_from_mongo_are_treated_as_utc():
    doc = {"next_allowed_at": (NOW + timedelta(seconds=9)).replace(tzinfo=None)}
    assert rl.evaluate_gate(doc, NOW).reason == "spaced"


# ── acquire / release against the stand-in ────────────────────────────────────

def test_first_post_gets_the_account_and_a_second_must_wait():
    db = FakeDb()
    assert _run(rl.acquire(db, KEY, "a", lease_s=300, now=NOW)).granted
    d = _run(rl.acquire(db, KEY, "b", lease_s=300, now=NOW + timedelta(seconds=1)))
    assert (d.granted, d.reason) == (False, "busy") and d.retry_at == NOW + timedelta(seconds=300)


def test_after_release_the_gap_applies_then_the_next_post_goes():
    db = FakeDb()
    _run(rl.acquire(db, KEY, "a", lease_s=300, now=NOW))
    _run(rl.release(db, KEY, "a", min_gap_s=12, now=NOW + timedelta(seconds=20)))
    d = _run(rl.acquire(db, KEY, "b", lease_s=300, now=NOW + timedelta(seconds=25)))
    assert (d.granted, d.reason) == (False, "spaced") and d.retry_at == NOW + timedelta(seconds=32)
    assert _run(rl.acquire(db, KEY, "b", lease_s=300, now=NOW + timedelta(seconds=33))).granted


def test_a_crashed_holder_does_not_block_forever():
    db = FakeDb()
    _run(rl.acquire(db, KEY, "a", lease_s=60, now=NOW))
    assert not _run(rl.acquire(db, KEY, "b", lease_s=60, now=NOW + timedelta(seconds=30))).granted
    assert _run(rl.acquire(db, KEY, "b", lease_s=60, now=NOW + timedelta(seconds=61))).granted


def test_only_the_owner_can_release():
    db = FakeDb()
    _run(rl.acquire(db, KEY, "a", lease_s=300, now=NOW))
    _run(rl.release(db, KEY, "someone-else", min_gap_s=5, now=NOW))
    assert db.gate.docs[KEY]["lease_owner"] == "a"


def test_different_accounts_do_not_block_each_other():
    db = FakeDb()
    assert _run(rl.acquire(db, "tiktok:tt_1", "a", lease_s=300, now=NOW)).granted
    assert _run(rl.acquire(db, "tiktok:tt_2", "b", lease_s=300, now=NOW)).granted


def test_account_key_shape():
    assert rl.account_key("instagram", "17841") == "instagram:17841"


# ── pause ─────────────────────────────────────────────────────────────────────

def test_pause_blocks_until_it_resumes_and_default_waits_grow_then_cap():
    db = FakeDb()
    resumes = [_run(rl.pause(db, KEY, code="17", reason="x", now=NOW)) for _ in range(4)]
    assert [(r - NOW).total_seconds() / 60 for r in resumes] == [5, 15, 60, 60]
    d = _run(rl.acquire(db, KEY, "a", lease_s=60, now=NOW + timedelta(minutes=30)))
    assert (d.granted, d.reason) == (False, "paused") and d.retry_at == resumes[-1]
    assert _run(rl.acquire(db, KEY, "a", lease_s=60, now=resumes[-1] + timedelta(seconds=1))).granted


def test_pause_until_a_time_the_platform_gave():
    db = FakeDb()
    until = NOW + timedelta(minutes=31)
    assert _run(rl.pause(db, KEY, until=until, code="80002", now=NOW)) == until


def test_clear_backoff_restarts_the_ladder():
    db = FakeDb()
    _run(rl.pause(db, KEY, now=NOW))
    _run(rl.pause(db, KEY, now=NOW))
    _run(rl.clear_backoff(db, KEY))
    assert (_run(rl.pause(db, KEY, now=NOW)) - NOW).total_seconds() / 60 == 5


# ── wait_for ──────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_wait_for_rides_out_a_short_gap():
    db, clock = FakeDb(), _Clock()
    _run(rl.acquire(db, KEY, "a", lease_s=300, now=NOW))
    _run(rl.release(db, KEY, "a", min_gap_s=5, now=NOW))
    d = _run(rl.wait_for(db, KEY, "b", max_wait_s=20, lease_s=300, sleep=clock.sleep, now_fn=clock))
    assert d.granted and clock.now >= NOW + timedelta(seconds=5)


def test_wait_for_gives_up_when_the_wait_would_be_too_long():
    db, clock = FakeDb(), _Clock()
    _run(rl.acquire(db, KEY, "a", lease_s=300, now=NOW))
    d = _run(rl.wait_for(db, KEY, "b", max_wait_s=20, lease_s=300, sleep=clock.sleep, now_fn=clock))
    assert (d.granted, d.reason) == (False, "busy") and d.retry_at == NOW + timedelta(seconds=300)
    assert clock.now == NOW  # it did not sit and wait for a 5-minute lease


def test_wait_for_does_not_wait_out_a_pause():
    db, clock = FakeDb(), _Clock()
    _run(rl.pause(db, KEY, until=NOW + timedelta(minutes=10), now=NOW))
    d = _run(rl.wait_for(db, KEY, "b", max_wait_s=20, sleep=clock.sleep, now_fn=clock))
    assert d.reason == "paused" and clock.now == NOW


def test_losing_a_race_reports_busy_not_a_false_grant():
    class RacyCollection(FakeGateCollection):
        async def find_one_and_update(self, *a, **k):
            raise DuplicateKeyError("lost the race")

    db = FakeDb()
    db.gate = RacyCollection()  # the document looks free when re-read
    d = _run(rl.acquire(db, KEY, "a", lease_s=60, now=NOW))
    assert not d.granted and d.reason == "busy"


# ── Optional: real Mongo, to prove cross-worker atomicity ─────────────────────

@pytest.mark.skipif(not os.getenv("MONGO_GATE_TEST_URI"), reason="set MONGO_GATE_TEST_URI to run against a real Mongo")
def test_only_one_of_many_concurrent_acquirers_wins_on_real_mongo():
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        client = AsyncIOMotorClient(os.environ["MONGO_GATE_TEST_URI"])
        db = client["gate_test"]
        await db[rl.GATE_COLLECTION].delete_many({})
        results = await asyncio.gather(*[rl.acquire(db, KEY, rl.new_owner(), lease_s=60) for _ in range(20)])
        await db[rl.GATE_COLLECTION].delete_many({})
        return sum(1 for r in results if r.granted)

    assert _run(go()) == 1
