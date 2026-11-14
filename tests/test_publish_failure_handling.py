"""
Publish failures, explained and acted on: a Facebook error is turned into plain words the owner
understands, a known "this login has lapsed" error flags the connection for reconnecting straight
away, and an unrecognised error only does so once it keeps happening on the same connection
(a success in between resets the count).
"""
import asyncio

import pytest

from app.agents.social_media_manager.services.approval_workflow_service import (
    _friendlier_facebook_error,
    _should_flag_connection,
    _record_publish_success,
    UNKNOWN_FAILURE_ESCALATION_THRESHOLD,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(self._docs, key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return [dict(d) for d in (self._docs[:length] if length is not None else self._docs)]


class FakeUpdateResult:
    def __init__(self, modified_count=0, matched_count=0):
        self.modified_count = modified_count
        self.matched_count = matched_count


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    def _match(self, query):
        if "$or" in query:
            return [d for d in self.docs if any(
                all(self._field_matches(d, k, v) for k, v in clause.items())
                for clause in query["$or"]
            )]
        return [d for d in self.docs if all(self._field_matches(d, k, v) for k, v in query.items())]

    def _field_matches(self, d, k, v):
        val = d.get(k)
        if isinstance(v, dict):
            if "$lte" in v:
                return val is not None and val <= v["$lte"]
            if "$ne" in v:
                return val != v["$ne"]
            if "$exists" in v:
                return (k in d) == v["$exists"]
            if "$in" in v:
                return val in v["$in"]
        return val == v

    async def find_one(self, query, projection=None, sort=None):
        matches = self._match(query)
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    def find(self, query):
        return FakeCursor(self._match(query))

    async def find_one_and_update(self, query, update, return_document=None, upsert=False):
        matches = self._match(query)
        if not matches:
            if not upsert:
                return None
            d = {}
            for k, v in query.items():
                if not isinstance(v, dict):
                    d[k] = v
            self.docs.append(d)
        else:
            d = matches[0]
        before = dict(d)
        d.update(update.get("$set", {}))
        for k, v in update.get("$inc", {}).items():
            d[k] = d.get(k, 0) + v
        # ReturnDocument.AFTER == 1 in pymongo; default (None/BEFORE) returns
        # the pre-update snapshot, matching real find_one_and_update semantics.
        return dict(d) if return_document else before

    async def update_one(self, query, update, upsert=False):
        matches = self._match(query)
        for d in matches:
            d.update(update.get("$set", {}))
            for k in update.get("$unset", {}):
                d.pop(k, None)
            return FakeUpdateResult(modified_count=1, matched_count=1)
        return FakeUpdateResult(modified_count=0, matched_count=0)

    async def update_many(self, query, update):
        matches = self._match(query)
        for d in matches:
            d.update(update.get("$set", {}))
        return FakeUpdateResult(modified_count=len(matches), matched_count=len(matches))


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


# ── _friendlier_facebook_error ───────────────────────────────────────────────

def test_friendly_error_explains_the_identity_checkpoint_in_plain_language():
    raw = "Error publishing post to Facebook: Failed to upload Facebook photo: 400 - Confirm your identity before you can publish as this Page. Open the Facebook app on your phone and follow the instructions."
    friendly = _friendlier_facebook_error(raw)
    assert "confirm your identity" in friendly.lower()
    assert "Facebook app on your phone" in friendly
    assert "reconnecting won't fix it" in friendly
    # Raw detail kept for support/debugging, not discarded.
    assert raw in friendly


def test_friendly_error_wraps_unrecognized_errors_in_plain_language():
    # An error with no catalogued pattern still gets a plain-language lead-in
    # immediately, on the very first occurrence — not raw passthrough, and
    # not held back pending any kind of threshold (that threshold only
    # gates whether the *connection* gets flagged for reconnect, a separate
    # decision from what the owner sees on this one post).
    raw = "Some other Outstand error we don't have a translation for"
    friendly = _friendlier_facebook_error(raw)
    assert friendly.startswith("This post couldn't be published to Facebook.")
    assert raw in friendly


def test_friendly_error_handles_missing_error_text():
    assert _friendlier_facebook_error(None) == "This post couldn't be published to Facebook. Facebook's detail: unknown error"
    assert _friendlier_facebook_error("") == "This post couldn't be published to Facebook. Facebook's detail: unknown error"


# ── _should_flag_connection / _record_publish_success ───────────────────────

def test_known_signature_escalates_on_the_very_first_failure():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    flag, reason = _run(_should_flag_connection(
        db=db, user_id="user-1", platform="facebook", connection=None,
        raw_error="Error validating access token: the session has been invalidated",
    ))
    assert flag is True
    assert "recognized error signature" in reason
    # A known signature never needs the counter at all — it's untouched.
    conn = db["social_connections"].docs[0]
    assert conn.get("consecutive_publish_failures", 0) == 0


def test_unrecognized_error_does_not_escalate_on_a_single_failure():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    flag, reason = _run(_should_flag_connection(
        db=db, user_id="user-1", platform="facebook", connection=None,
        raw_error="Some brand-new Outstand error nobody has seen before",
    ))
    assert flag is False
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 1


def test_unrecognized_error_escalates_after_the_threshold_is_reached():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    results = []
    for _ in range(UNKNOWN_FAILURE_ESCALATION_THRESHOLD):
        results.append(_run(_should_flag_connection(
            db=db, user_id="user-1", platform="facebook", connection=None,
            raw_error="Some brand-new Outstand error nobody has seen before",
        )))
    # Every failure before the threshold does NOT escalate; only hitting the
    # threshold does — a single one-off failure must never trigger a
    # "reconnect your account" alert on its own.
    assert [flag for flag, _ in results[:-1]] == [False] * (UNKNOWN_FAILURE_ESCALATION_THRESHOLD - 1)
    assert results[-1][0] is True
    assert "consecutive unrecognized" in results[-1][1]


def test_a_success_in_between_resets_the_unrecognized_failure_count():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    _run(_should_flag_connection(db=db, user_id="user-1", platform="facebook", connection=None, raw_error="unknown issue"))
    _run(_should_flag_connection(db=db, user_id="user-1", platform="facebook", connection=None, raw_error="unknown issue"))
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 2
    _run(_record_publish_success(db=db, user_id="user-1", platform="facebook", connection=None))
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 0
    # Counting starts fresh after the reset, not from where it left off.
    flag, _ = _run(_should_flag_connection(db=db, user_id="user-1", platform="facebook", connection=None, raw_error="unknown issue"))
    assert flag is False
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
