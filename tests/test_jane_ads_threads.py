"""
Unit tests for the pure thread helpers (threads.py) — id shape, title/preview trimming,
and the duplicate-campaign seed message. The Mongo read/writes are thin wrappers covered
live; these cover the logic that decides what a thread is labeled and duplicated as.
"""
from app.agents.jane_ads.threads import (
    new_thread_id, seed_message_from_campaign, title_from_message,
)


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
