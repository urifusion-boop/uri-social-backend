"""
Unit tests for the pure thread helpers (threads.py) — id shape, title/preview trimming,
and the duplicate-campaign seed message. The Mongo read/writes are thin wrappers covered
live; these cover the logic that decides what a thread is labeled and duplicated as.
"""
import asyncio

from app.agents.jane_ads.threads import (
    CHAT_COLLECTION, THREADS_COLLECTION,
    delete_thread, new_thread_id, seed_message_from_campaign, title_from_message,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _DeleteResult:
    def __init__(self, count):
        self.deleted_count = count


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def delete_one(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in query.items())]
        return _DeleteResult(before - len(self.docs))

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in query.items())]
        return _DeleteResult(before - len(self.docs))


class FakeDb:
    def __init__(self, threads=None, messages=None):
        self._colls = {THREADS_COLLECTION: FakeCollection(threads), CHAT_COLLECTION: FakeCollection(messages)}

    def __getitem__(self, name):
        return self._colls[name]


def test_delete_thread_removes_thread_and_its_messages():
    db = FakeDb(
        threads=[{"brand_id": "brnd_1", "thread_id": "thr_a"}, {"brand_id": "brnd_1", "thread_id": "thr_b"}],
        messages=[{"brand_id": "brnd_1", "thread_id": "thr_a"}, {"brand_id": "brnd_1", "thread_id": "thr_a"},
                  {"brand_id": "brnd_1", "thread_id": "thr_b"}],
    )
    deleted = _run(delete_thread(db, "brnd_1", "thr_a"))
    assert deleted is True
    assert db[THREADS_COLLECTION].docs == [{"brand_id": "brnd_1", "thread_id": "thr_b"}]
    assert db[CHAT_COLLECTION].docs == [{"brand_id": "brnd_1", "thread_id": "thr_b"}]


def test_delete_thread_returns_false_for_a_different_brand():
    db = FakeDb(threads=[{"brand_id": "brnd_1", "thread_id": "thr_a"}])
    deleted = _run(delete_thread(db, "brnd_other", "thr_a"))
    assert deleted is False
    assert len(db[THREADS_COLLECTION].docs) == 1  # untouched


def test_thread_id_shape():
    tid = new_thread_id()
    assert tid.startswith("thr_") and len(tid) == len("thr_") + 16


def test_thread_ids_unique():
    assert new_thread_id() != new_thread_id()


def test_title_from_first_message_is_trimmed():
    long = "I want to run an ad for my restaurant in Surulere targeting young professionals nearby"
    t = title_from_message(long)
    assert len(t) <= 48 and t.startswith("I want to run an ad")


def test_title_defaults_when_empty():
    assert title_from_message("") == "New campaign"
    assert title_from_message("   ") == "New campaign"


def test_title_collapses_whitespace():
    assert title_from_message("hello   there\n\nfriend") == "hello there friend"


def test_seed_message_rebuilds_brief_from_campaign():
    camp = {"name": "Mama Kitchen", "goal": "messages", "budget_ngn": 20000, "city": "Surulere"}
    seed = seed_message_from_campaign(camp)
    assert "Mama Kitchen" in seed
    assert "20,000" in seed
    assert "Surulere" in seed


def test_seed_message_falls_back_when_nothing_stored():
    assert seed_message_from_campaign({}) == "Run another ad like my last one"
