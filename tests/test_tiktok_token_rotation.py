"""
TikTok replaces the refresh token on every refresh and invalidates the old one (and, from
1 Dec 2026, access tokens last 2 hours, so refreshes are ~12x as frequent). These tests pin the
two ways that breaks a connection:

  * two refreshes with the same refresh token — the second fails and the user must reconnect;
  * a refresh TikTok accepted whose new tokens never reach the database.

The fake TikTok below enforces the rotation, so a test only passes if the code really never
reuses a replaced refresh token.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.social_media_manager.services import tiktok_direct_service as svc
from app.agents.social_media_manager.services.tiktok_direct_service import TikTokDirectAPIError

CONN_ID = "tt_open_1"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def iso(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


class FakeCollection:
    """Just enough of a Mongo collection for the connection document, including the conditional
    writes the refresh claim depends on."""

    def __init__(self, docs):
        self.docs = {d["id"]: dict(d) for d in docs}
        self.fail_saves = 0           # how many token writes to refuse before accepting
        self.save_attempts = 0

    @staticmethod
    def _match(doc, flt):
        for key, want in flt.items():
            if key == "$or":
                if not any(FakeCollection._match(doc, sub) for sub in want):
                    return False
            elif isinstance(want, dict):
                have = doc.get(key)
                for op, operand in want.items():
                    if op == "$exists" and (key in doc) != operand:
                        return False
                    if op == "$lte" and not (key in doc and have <= operand):
                        return False
            elif doc.get(key) != want:
                return False
        return True

    async def find_one(self, flt, projection=None):
        await asyncio.sleep(0)
        for doc in self.docs.values():
            if self._match(doc, flt):
                return {k: v for k, v in doc.items() if k != "_id"}
        return None

    async def update_one(self, flt, update):
        await asyncio.sleep(0)
        if "access_token" in update.get("$set", {}):
            self.save_attempts += 1
            if self.fail_saves > 0:
                self.fail_saves -= 1
                raise ConnectionError("database unavailable")
        for doc in self.docs.values():
            if self._match(doc, flt):
                before = dict(doc)
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return type("R", (), {"modified_count": int(doc != before)})()
        return type("R", (), {"modified_count": 0})()


class FakeDB:
    def __init__(self, doc):
        self.col = FakeCollection([doc])

    def __getitem__(self, name):
        assert name == svc.CONNECTIONS
        return self.col

    @property
    def doc(self):
        return self.col.docs[CONN_ID]


def expired_doc(**extra):
    return {
        "id": CONN_ID, "platform": "tiktok", "connected_via": "tiktok_direct_oauth",
        "access_token": "a0", "refresh_token": "r0", "token_expires_at": iso(-60), **extra,
    }


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch):
    monkeypatch.setattr(svc, "_REFRESH_POLL_SECONDS", 0.005)
    monkeypatch.setattr(svc, "_SAVE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(svc, "_REFRESH_WAIT_SECONDS", 0.4)
    svc._unsaved_tokens.clear()
    yield
    svc._unsaved_tokens.clear()


@pytest.fixture
def tiktok(monkeypatch):
    """A TikTok that rotates: each refresh needs the CURRENT refresh token and replaces it."""
    state = {"valid_refresh": "r0", "n": 0, "calls": 0, "fail_with": None}

    async def fake_refresh(refresh_token):
        state["calls"] += 1
        await asyncio.sleep(0.01)                   # real latency, so callers overlap
        if state["fail_with"]:
            raise state["fail_with"]
        if refresh_token != state["valid_refresh"]:
            raise TikTokDirectAPIError("token refresh failed: refresh token is invalid or expired")
        state["n"] += 1
        state["valid_refresh"] = f"r{state['n']}"
        return {
            "access_token": f"a{state['n']}", "refresh_token": f"r{state['n']}",
            "expires_in": 7200, "refresh_expires_in": 15552000,
        }

    monkeypatch.setattr(svc, "_refresh_tokens", fake_refresh)
    return state


# ── a normal refresh ──────────────────────────────────────────────────────────

def test_a_token_that_is_still_good_is_used_without_refreshing(tiktok):
    db = FakeDB(expired_doc(token_expires_at=iso(3600)))
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a0"
    assert tiktok["calls"] == 0


def test_a_refresh_saves_the_new_tokens_and_both_expiries(tiktok):
    db = FakeDB(expired_doc())
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"
    saved = db.doc
    assert (saved["access_token"], saved["refresh_token"]) == ("a1", "r1")
    # The access token's life comes from TikTok's expires_in (2h), not an assumed 24h.
    remaining = (datetime.fromisoformat(saved["token_expires_at"]) - datetime.now(timezone.utc)).total_seconds()
    assert 7000 < remaining <= 7200
    assert "refresh_token_expires_at" in saved
    assert "refresh_lock_owner" not in saved and "refresh_lock_until" not in saved


def test_no_refresh_token_means_reconnect(tiktok):
    doc = expired_doc()
    doc["refresh_token"] = ""
    db = FakeDB(doc)
    with pytest.raises(TikTokDirectAPIError, match="reconnect"):
        run(svc.get_valid_tiktok_access_token(db, db.doc))


# ── two refreshes at once ────────────────────────────────────────────────────

def test_simultaneous_callers_trigger_one_refresh_and_share_its_result(tiktok):
    db = FakeDB(expired_doc())
    stale = db.doc                                   # every caller holds the same stale document

    async def go():
        return await asyncio.gather(*[svc.get_valid_tiktok_access_token(db, dict(stale)) for _ in range(6)])

    tokens = run(go())
    assert tiktok["calls"] == 1
    assert set(tokens) == {"a1"}
    assert db.doc["refresh_token"] == "r1"


def test_a_caller_with_an_old_document_picks_up_another_workers_refresh(tiktok):
    db = FakeDB(expired_doc())
    stale = db.doc
    # Another worker refreshed after this caller read its copy.
    db.col.docs[CONN_ID].update(access_token="a1", refresh_token="r1", token_expires_at=iso(7000))
    tiktok["valid_refresh"], tiktok["n"] = "r1", 1
    assert run(svc.get_valid_tiktok_access_token(db, dict(stale))) == "a1"
    assert tiktok["calls"] == 0


def test_a_waiter_gives_up_with_a_clear_error_if_the_refresh_never_finishes(tiktok):
    # Another worker holds a live claim and never saves.
    db = FakeDB(expired_doc(refresh_lock_owner="other", refresh_lock_until=iso(600)))
    with pytest.raises(TikTokDirectAPIError, match="still refreshing"):
        run(svc.get_valid_tiktok_access_token(db, db.doc))
    assert tiktok["calls"] == 0


def test_a_claim_left_by_a_crashed_worker_expires(tiktok):
    db = FakeDB(expired_doc(refresh_lock_owner="dead", refresh_lock_until=iso(-5)))
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"
    assert tiktok["calls"] == 1


def test_a_refresh_that_fails_frees_the_claim_for_the_next_caller(tiktok):
    db = FakeDB(expired_doc())
    tiktok["fail_with"] = TikTokDirectAPIError("TikTok is down")
    with pytest.raises(TikTokDirectAPIError, match="TikTok is down"):
        run(svc.get_valid_tiktok_access_token(db, db.doc))
    assert "refresh_lock_owner" not in db.doc
    tiktok["fail_with"] = None
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"


# ── a refresh TikTok accepted but the database did not ───────────────────────

def test_a_failed_save_is_retried(tiktok):
    db = FakeDB(expired_doc())
    db.col.fail_saves = 2
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"
    assert db.doc["refresh_token"] == "r1"
    assert tiktok["calls"] == 1
    assert not svc._unsaved_tokens


def test_when_the_database_stays_down_the_new_tokens_are_kept_and_saved_later(tiktok):
    db = FakeDB(expired_doc())
    db.col.fail_saves = 99
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"
    assert db.doc["refresh_token"] == "r0"           # the database still has the dead token
    assert svc._unsaved_tokens[CONN_ID]["refresh_token"] == "r1"

    # Still down: the next call uses the held tokens rather than refreshing with the dead one.
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"
    assert tiktok["calls"] == 1

    # Back up: the held tokens are written, the connection survives, and nothing is refreshed again.
    db.col.fail_saves = 0
    assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == "a1"
    assert db.doc["refresh_token"] == "r1"
    assert not svc._unsaved_tokens
    assert tiktok["calls"] == 1


def test_the_old_refresh_token_is_never_used_twice(tiktok):
    """The fake TikTok refuses a replaced token, so any reuse fails the whole sequence."""
    db = FakeDB(expired_doc())
    for expected in ("a1", "a2", "a3"):
        db.col.docs[CONN_ID]["token_expires_at"] = iso(-60)     # let it expire again
        assert run(svc.get_valid_tiktok_access_token(db, db.doc)) == expected
    assert db.doc["refresh_token"] == "r3"


def test_a_claim_made_with_a_replaced_refresh_token_is_refused(tiktok):
    """Between reading the connection and claiming it, another worker may finish its refresh. The
    late caller's refresh token is then dead, and a refresh with it would fail at TikTok — and must
    never be started."""
    db = FakeDB(expired_doc(refresh_token="r1"))              # the database has already moved on
    assert run(svc._claim_refresh(db, CONN_ID, "r0")) is None
    assert "refresh_lock_owner" not in db.doc
    assert run(svc._claim_refresh(db, CONN_ID, "r1")) is not None


def test_only_one_claim_is_granted_at_a_time(tiktok):
    db = FakeDB(expired_doc())
    first = run(svc._claim_refresh(db, CONN_ID, "r0"))
    assert first is not None
    assert run(svc._claim_refresh(db, CONN_ID, "r0")) is None
