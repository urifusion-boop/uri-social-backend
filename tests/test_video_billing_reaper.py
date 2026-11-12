"""
Credits taken for a run that a server restart cut short are given back, once, and a healthy slow run is
never touched.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.agents.social_media_manager.services import video_generation_billing as B
from tests.test_video_generation_billing import FakeCol, FakeTrial, FakeWallet, on  # noqa: F401 (on = fixture)

NOW = datetime(2026, 10, 30, 12, 0, 0)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return [dict(d) for d in self.docs][:length]


class ReapCol(FakeCol):
    """FakeCol plus find() with the one operator the sweep uses ($in on status)."""
    def _match(self, d, flt):
        for k, v in flt.items():
            if isinstance(v, dict) and "$in" in v:
                if d.get(k) not in v["$in"]:
                    return False
            elif d.get(k) != v:
                return False
        return True

    def find(self, flt, *a, **kw):
        return Cursor([d for d in self.docs if self._match(d, flt)])


class Jobs(ReapCol):
    def __init__(self, *docs):
        super().__init__()
        self.docs = [dict(d) for d in docs]


def _charged_video(wallet, col, age_minutes, scenes=3, credits=3):
    out = _run(B.start("u1", {"scenes": [{"duration_seconds": 5}] * scenes}, "quick_video", "Sarah", None,
                       credits=wallet, trials=FakeTrial(), col=col))
    col.one(out["job_id"])["created_at"] = NOW - timedelta(minutes=age_minutes)
    return out["job_id"]


def _charged_storyboard(wallet, col, age_minutes):
    out = _run(B.start_storyboard("u1", None, credits=wallet, trials=FakeTrial(), col=col))
    col.one(out["job_id"])["created_at"] = NOW - timedelta(minutes=age_minutes)
    return out["job_id"]


def _reap(wallet, col, video=None, storyboard=None):
    return _run(B.reap_stranded(now=NOW, col=col, video_jobs=video or Jobs(), storyboard_jobs=storyboard or Jobs(),
                                credits=wallet, trials=FakeTrial()))


# ── storyboards ───────────────────────────────────────────────────────────────

def test_a_storyboard_whose_run_was_lost_gets_its_credit_back_and_the_job_ends(on):
    wallet, col = FakeWallet(5), ReapCol()
    job_id = _charged_storyboard(wallet, col, age_minutes=45)
    jobs = Jobs({"job_id": job_id, "status": "generating", "storyboard": None})
    assert _reap(wallet, col, storyboard=jobs) == {"checked": 1, "storyboards": 1, "videos": 0, "needs_review": 0}
    assert wallet.credits == 5 and len(wallet.refunds) == 1
    assert jobs.docs[0]["status"] == "failed" and "Your credits" in jobs.docs[0]["error"]
    assert col.one(job_id)["status"] == "settled"


def test_a_storyboard_that_is_only_slow_is_left_alone(on):
    wallet, col = FakeWallet(5), ReapCol()
    job_id = _charged_storyboard(wallet, col, age_minutes=5)
    jobs = Jobs({"job_id": job_id, "status": "generating", "storyboard": None})
    assert _reap(wallet, col, storyboard=jobs)["storyboards"] == 0
    assert wallet.credits == 4 and col.one(job_id)["status"] == "charged" and jobs.docs[0]["status"] == "generating"


def test_a_storyboard_that_finished_but_was_never_settled_keeps_its_credit(on):
    wallet, col = FakeWallet(5), ReapCol()
    job_id = _charged_storyboard(wallet, col, age_minutes=45)
    jobs = Jobs({"job_id": job_id, "status": "complete", "storyboard": {"scenes": [1]}})
    _reap(wallet, col, storyboard=jobs)
    assert wallet.credits == 4 and wallet.refunds == []
    assert jobs.docs[0]["status"] == "complete"


def test_a_storyboard_whose_job_record_is_missing_is_refunded(on):
    wallet, col = FakeWallet(5), ReapCol()
    _charged_storyboard(wallet, col, age_minutes=45)
    _reap(wallet, col)
    assert wallet.credits == 5


# ── videos ────────────────────────────────────────────────────────────────────

def test_a_video_lost_with_no_scenes_done_is_refunded_in_full_and_ends(on):
    wallet, col = FakeWallet(10), ReapCol()
    job_id = _charged_video(wallet, col, age_minutes=300)
    jobs = Jobs({"job_id": job_id, "status": "generating", "total_scenes": 3, "clips": []})
    assert _reap(wallet, col, video=jobs)["videos"] == 1
    assert wallet.credits == 10
    assert jobs.docs[0]["status"] == "failed"


def test_a_video_lost_halfway_keeps_the_share_that_was_delivered(on):
    wallet, col = FakeWallet(10), ReapCol()
    job_id = _charged_video(wallet, col, age_minutes=300, scenes=2)
    clips = [{"video_url": "https://v/1.mp4", "cost_usd": 0.1}, {"video_url": None}]
    jobs = Jobs({"job_id": job_id, "status": "generating", "total_scenes": 2, "clips": clips})
    _reap(wallet, col, video=jobs)
    charged = col.one(job_id)["credits_charged"]
    refunded = col.one(job_id)["credits_refunded"]
    assert 0 < refunded < charged and wallet.credits == 10 - charged + refunded


def test_a_long_running_healthy_video_is_not_touched(on):
    wallet, col = FakeWallet(10), ReapCol()
    job_id = _charged_video(wallet, col, age_minutes=40)   # a talking video can legitimately take this long
    jobs = Jobs({"job_id": job_id, "status": "generating", "total_scenes": 3, "clips": []})
    assert _reap(wallet, col, video=jobs)["videos"] == 0
    assert wallet.refunds == [] and jobs.docs[0]["status"] == "generating"


def test_a_video_whose_job_record_is_missing_is_refunded_in_full(on):
    wallet, col = FakeWallet(10), ReapCol()
    _charged_video(wallet, col, age_minutes=300)
    _reap(wallet, col)
    assert wallet.credits == 10


# ── safety ────────────────────────────────────────────────────────────────────

def test_running_the_sweep_twice_never_refunds_twice(on):
    wallet, col = FakeWallet(5), ReapCol()
    _charged_storyboard(wallet, col, age_minutes=45)
    _reap(wallet, col)
    _reap(wallet, col)
    assert wallet.credits == 5 and len(wallet.refunds) == 1


def test_a_record_stuck_while_settling_is_flagged_not_guessed_at(on):
    wallet, col = FakeWallet(5), ReapCol()
    job_id = _charged_storyboard(wallet, col, age_minutes=120)
    col.one(job_id)["status"] = "settling"
    assert _reap(wallet, col)["needs_review"] == 1
    assert col.one(job_id)["status"] == "needs_review" and wallet.refunds == []


def test_a_settlement_that_just_started_is_left_alone(on):
    wallet, col = FakeWallet(5), ReapCol()
    job_id = _charged_storyboard(wallet, col, age_minutes=2)
    col.one(job_id)["status"] = "settling"
    assert _reap(wallet, col)["needs_review"] == 0 and col.one(job_id)["status"] == "settling"


def test_already_settled_records_are_ignored(on):
    wallet, col = FakeWallet(5), ReapCol()
    job_id = _charged_storyboard(wallet, col, age_minutes=45)
    _run(B.settle_storyboard(job_id, True, credits=wallet, trials=FakeTrial(), col=col))
    assert _reap(wallet, col)["checked"] == 0
