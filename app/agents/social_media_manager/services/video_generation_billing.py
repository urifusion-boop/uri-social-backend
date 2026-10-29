"""
Credits for AI video generation: quote it, charge it once, give back what was not delivered.

Money flow for one generated video:
  1. quote   - the server prices the storyboard it is about to render (video_pricing.py).
  2. charge  - the credits are taken BEFORE any provider work starts, from the trial balance for a
               trial user and from the paid wallet otherwise (never both, as video editing does).
  3. render  - the job runs; automatic retries and fallback models are our cost, not a second charge.
  4. settle  - once, when the job ends: scenes that never came out are refunded in proportion, and
               a job that produced nothing, or crashed, is refunded in full.

Everything is recorded in `video_generation_billing`, one document per job, so an operator can see
exactly what was charged and returned, and a refund that could not be paid is left marked for follow-up
instead of being lost.

Writing a storyboard is charged the same way, separately and before the video: STORYBOARD_CREDITS each time,
given back in full when no storyboard comes out (start_storyboard / settle_storyboard below).

Mode (settings.VIDEO_GENERATION_BILLING_MODE):
  off     nothing is quoted, charged or recorded; generation is free exactly as before.
  shadow  prices and records what WOULD be charged, charges nothing. For a pilot: compare the recorded
          prices with real provider costs before turning charging on.
  on      real charging.
"""
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from pymongo.errors import DuplicateKeyError

from app.agents.social_media_manager.services import video_pricing
from app.core.config import settings
from app.database import get_db

COLLECTION = "video_generation_billing"
DEDUCT_REASON = "video_generation"      # must be a reason CreditTransaction accepts
UPGRADE_URL = "/pricing"

_index_ready = False


def mode() -> str:
    value = (getattr(settings, "VIDEO_GENERATION_BILLING_MODE", "off") or "off").strip().lower()
    return value if value in ("off", "shadow", "on") else "off"


def _collection():
    return get_db()[COLLECTION]


async def _ensure_index(col) -> None:
    """One record per (user, idempotency key): a double click or a replayed request can't be charged twice."""
    global _index_ready
    if _index_ready:
        return
    await col.create_index([("user_id", 1), ("idempotency_key", 1)], unique=True, sparse=True)
    await col.create_index([("job_id", 1)], unique=True)
    _index_ready = True


class BillingDeclined(Exception):
    """The customer cannot pay for this video (not enough credits). Carries what the API should say."""

    def __init__(self, message: str, details: Dict[str, Any]):
        super().__init__(message)
        self.message, self.details = message, details


def _services(credits=None, trials=None):
    if credits is None:
        from app.services.CreditService import credit_service as credits
    if trials is None:
        from app.services.TrialService import trial_service as trials
    return credits, trials


async def _balance(user_id: str, credits, trials) -> Dict[str, Any]:
    """Which wallet pays, and what is in it. `remaining` is None for a legacy account with no wallet."""
    if await trials.has_active_trial(user_id):
        status = await trials.get_trial_status(user_id)
        return {"is_trial": True, "remaining": status.credits_remaining}
    wallet = await credits.get_user_wallet(user_id)
    return {"is_trial": False, "remaining": None if wallet is None else wallet.credits_remaining}


async def quote(user_id: str, storyboard: dict, outcome: str, avatar_voice: str = "",
                credits=None, trials=None) -> Dict[str, Any]:
    """What this video would cost, and whether the customer can afford it. Raises PricingError if unpriceable."""
    current = mode()
    if current == "off":
        return {"billing": "off"}
    credits, trials = _services(credits, trials)
    priced = video_pricing.quote_for(storyboard, outcome, avatar_voice)
    balance = await _balance(user_id, credits, trials)
    remaining = balance["remaining"]
    needed = priced["credits_required"]
    return {
        "billing": current,
        **priced,
        "balance": remaining,
        "balance_after": None if remaining is None else max(0, remaining - needed),
        "sufficient": True if remaining is None else remaining >= needed,
        "is_trial": balance["is_trial"],
    }


async def start(user_id: str, storyboard: dict, outcome: str, avatar_voice: str = "",
                idempotency_key: Optional[str] = None, credits=None, trials=None,
                col=None) -> Dict[str, Any]:
    """Price the video and take the credits. Returns {"job_id", "billing", "existing"}.

    `existing` is True when this idempotency key was already used: the original job id comes back and
    nobody is charged again. Raises BillingDeclined when the customer cannot afford it."""
    current = mode()
    job_id = uuid.uuid4().hex
    if current == "off":
        return {"job_id": job_id, "billing": None, "existing": False}

    # Price it first: a video that cannot be priced (too long, nothing in it) is refused before anything is
    # recorded or any database is touched.
    priced = video_pricing.quote_for(storyboard, outcome, avatar_voice)
    return await _charge(job_id, user_id, priced, idempotency_key, current, credits, trials, col)


async def _charge(job_id: str, user_id: str, priced: Dict[str, Any], idempotency_key: Optional[str],
                  current: str, credits, trials, col, what: str = "video") -> Dict[str, Any]:
    """Record and take the credits for something already priced (`priced["credits_required"]`)."""
    needed = priced["credits_required"]
    credits, trials = _services(credits, trials)
    col = col if col is not None else _collection()
    await _ensure_index(col)

    record: Dict[str, Any] = {
        "job_id": job_id, "user_id": user_id, "mode": current, **priced,
        "status": "charging", "credits_charged": 0, "credits_refunded": 0,
        "created_at": datetime.utcnow(),
    }
    if idempotency_key:
        record["idempotency_key"] = idempotency_key[:80]
    try:
        await col.insert_one(record)
    except DuplicateKeyError:
        original = await col.find_one({"user_id": user_id, "idempotency_key": record.get("idempotency_key")})
        if original:
            return {"job_id": original["job_id"], "billing": _public(original), "existing": True}
        raise

    if current == "shadow":
        await col.update_one({"job_id": job_id}, {"$set": {"status": "shadow", "would_charge": needed}})
        return {"job_id": job_id, "billing": {"mode": "shadow", "credits_required": needed}, "existing": False}

    balance = await _balance(user_id, credits, trials)
    if not balance["is_trial"] and balance["remaining"] is None:
        # An older account with no wallet is unmetered for every other feature too (deduct_credit lets it
        # through without taking anything). Record the job as free so settling never tries to refund a
        # charge that was not made.
        await col.update_one({"job_id": job_id}, {"$set": {"status": "charged", "credits_charged": 0,
                                                              "unmetered": True, "is_trial": False}})
        return {"job_id": job_id, "existing": False,
                "billing": {"mode": "on", "credits_charged": 0, "credits_refunded": 0, "status": "charged"}}
    if balance["is_trial"]:
        paid = await trials.deduct_trial_credit(user_id=user_id, campaign_id=job_id, reason=DEDUCT_REASON, amount=needed)
    else:
        paid = await credits.deduct_credit(user_id=user_id, campaign_id=job_id, reason=DEDUCT_REASON, amount=needed)
    if not paid:
        # Free the idempotency key so a retry after topping up is a fresh attempt.
        await col.update_one({"job_id": job_id}, {"$set": {"status": "declined"}, "$unset": {"idempotency_key": ""}})
        remaining = (await _balance(user_id, credits, trials))["remaining"] or 0
        raise BillingDeclined(
            f"This {what} needs {needed} credit{'s' if needed != 1 else ''}, but you have {remaining}. Top up to continue.",
            {"credits_required": needed, "credits_remaining": remaining, "upgrade_url": UPGRADE_URL},
        )
    await col.update_one(
        {"job_id": job_id},
        {"$set": {"status": "charged", "credits_charged": needed, "is_trial": balance["is_trial"],
                  "charged_at": datetime.utcnow()}},
    )
    return {
        "job_id": job_id, "existing": False,
        "billing": {"mode": "on", "credits_charged": needed, "credits_refunded": 0, "status": "charged"},
    }


async def quote_storyboard(user_id: str, credits=None, trials=None) -> Dict[str, Any]:
    """What writing a storyboard costs, and whether the customer can afford it."""
    current = mode()
    if current == "off":
        return {"billing": "off"}
    credits, trials = _services(credits, trials)
    needed = video_pricing.STORYBOARD_CREDITS
    balance = await _balance(user_id, credits, trials)
    remaining = balance["remaining"]
    return {
        "billing": current, "credits_required": needed,
        "balance": remaining,
        "balance_after": None if remaining is None else max(0, remaining - needed),
        "sufficient": True if remaining is None else remaining >= needed,
        "is_trial": balance["is_trial"],
    }


async def start_storyboard(user_id: str, idempotency_key: Optional[str] = None, credits=None, trials=None,
                           col=None) -> Dict[str, Any]:
    """Take the credit for one storyboard run, before any writing starts. Same contract as start():
    {"job_id", "billing", "existing"}; raises BillingDeclined when the customer cannot afford it.

    The key is namespaced so a storyboard key can never collide with a video key from the same browser."""
    current = mode()
    job_id = uuid.uuid4().hex
    if current == "off":
        return {"job_id": job_id, "billing": None, "existing": False}
    priced = {"kind": "storyboard", "scenes": 1, "credits_required": video_pricing.STORYBOARD_CREDITS,
              "pricing_version": video_pricing.PRICING_VERSION}
    key = f"sb:{idempotency_key}" if idempotency_key else None
    return await _charge(job_id, user_id, priced, key, current, credits, trials, col, what="storyboard")


async def settle_storyboard(job_id: str, succeeded: bool, credits=None, trials=None, col=None) -> Optional[dict]:
    """The storyboard run ended: keep the credit if a storyboard came out, give it back if not. Safe to call
    twice, and a no-op for a run that was never charged."""
    job = {"total_scenes": 1, "clips": [{"video_url": "storyboard"}] if succeeded else []}
    return await settle(job_id, job=job, credits=credits, trials=trials, col=col)


def _public(record: Optional[dict]) -> Optional[dict]:
    """The part of a billing record a customer may see."""
    if not record:
        return None
    return {
        "mode": record.get("mode"), "status": record.get("status"),
        "credits_charged": record.get("credits_charged", 0),
        "credits_refunded": record.get("credits_refunded", 0),
        "credits_required": record.get("credits_required"),
    }


async def settle(job_id: str, job: Optional[dict] = None, credits=None, trials=None, col=None) -> Optional[dict]:
    """Close the books on a finished job: refund scenes that were not delivered. Safe to call twice.

    Only the call that wins the "charged" -> "settling" switch does anything, so a repeated or concurrent
    call can neither refund twice nor skip a refund."""
    col = col if col is not None else _collection()
    record = await col.find_one({"job_id": job_id})
    if not record or record.get("status") not in ("charged", "shadow"):
        return _public(record)
    claimed = await col.find_one_and_update(
        {"job_id": job_id, "status": record["status"]}, {"$set": {"status": "settling"}},
    )
    if not claimed:
        return _public(await col.find_one({"job_id": job_id}))

    try:
        if job is None:
            job = await get_db()["video_generation_jobs"].find_one({"job_id": job_id}) or {}
        clips = job.get("clips") or []
        total = int(job.get("total_scenes") or record.get("scenes") or 0)
        delivered = sum(1 for clip in clips if clip.get("video_url"))
        charged = int(record.get("credits_charged") or 0)
        refund = video_pricing.refund_for_failed_scenes(charged, total, delivered)
        provider_cost = round(sum(float(c.get("cost_usd") or 0) for c in clips), 4)
        fallbacks = sum(1 for c in clips if c.get("fallback_used"))

        final: Dict[str, Any] = {
            "status": "settled", "scenes_delivered": delivered, "scenes_total": total,
            "provider_cost_usd": provider_cost, "fallbacks_used": fallbacks, "settled_at": datetime.utcnow(),
        }
        if record["status"] == "shadow":
            final["status"] = "shadow_settled"
            final["would_refund"] = video_pricing.refund_for_failed_scenes(record.get("would_charge", 0), total, delivered)
        elif refund > 0:
            credits, trials = _services(credits, trials)
            reason = "refund"
            try:
                if record.get("is_trial"):
                    ok = await trials.refund_trial_credit(user_id=record["user_id"], campaign_id=job_id, reason=reason, amount=refund)
                else:
                    ok = await credits.refund_credit(user_id=record["user_id"], campaign_id=job_id, reason=reason, amount=refund)
            except Exception as e:  # never lose track of money owed
                print(f"[VideoBilling] refund of {refund} for job {job_id} FAILED: {type(e).__name__}: {e}", flush=True)
                ok = False
            if ok:
                final["credits_refunded"] = refund
            else:
                final["status"] = "refund_failed"
                final["refund_due"] = refund
        await col.update_one({"job_id": job_id}, {"$set": final})
        return _public(await col.find_one({"job_id": job_id}))
    except Exception as e:
        print(f"[VideoBilling] settling job {job_id} failed: {type(e).__name__}: {e}", flush=True)
        await col.update_one({"job_id": job_id}, {"$set": {"status": "settle_failed", "error": str(e)[:300]}})
        raise


# ── Credits stranded by a restart ────────────────────────────────────────────────────────────────────────────
# A video or storyboard job runs inside the server process. If the server restarts mid-run the job is lost and
# nothing ever settles it, so its credits would stay taken for work that never finished. This sweep, run every
# few minutes by the scheduler, finds charged records whose job stopped moving and settles them the same way a
# normal ending would: delivered scenes keep their share, the rest is returned. The thresholds are well above the
# longest a healthy run takes, so a run that is simply slow is never touched.

STORYBOARD_STALE_AFTER = timedelta(minutes=20)
VIDEO_STALE_AFTER = timedelta(minutes=120)     # a talking clip alone can take half an hour
SETTLING_STALE_AFTER = timedelta(minutes=30)
INTERRUPTED_MESSAGE = "This was interrupted before it finished. Your credits for the unfinished part were returned."
_RUNNING = ["queued", "generating"]


async def reap_stranded(now: Optional[datetime] = None, col=None, video_jobs=None, storyboard_jobs=None,
                        credits=None, trials=None) -> Dict[str, int]:
    """Settle charged jobs whose run was lost. Returns counts, for the log. Safe to run on every worker at once:
    settle() lets exactly one caller act on a record."""
    now = now or datetime.utcnow()
    col = col if col is not None else _collection()
    video_jobs = video_jobs if video_jobs is not None else get_db()["video_generation_jobs"]
    storyboard_jobs = storyboard_jobs if storyboard_jobs is not None else get_db()["creative_storyboard_jobs"]
    counts = {"checked": 0, "storyboards": 0, "videos": 0, "needs_review": 0}

    for record in await col.find({"status": {"$in": ["charged", "settling"]}, "mode": "on"}).to_list(length=200):
        age = now - (record.get("created_at") or now)
        job_id = record["job_id"]
        if record["status"] == "settling":
            # A process died between claiming the settlement and recording it. Whether the refund was paid is
            # not known, so it is NOT guessed (that could pay it twice): it is flagged for a person to look at.
            if age > SETTLING_STALE_AFTER:
                await col.update_one({"job_id": job_id, "status": "settling"}, {"$set": {"status": "needs_review"}})
                counts["needs_review"] += 1
                print(f"[VideoBilling] job {job_id} stuck while settling - needs a manual look", flush=True)
            continue
        counts["checked"] += 1

        if record.get("kind") == "storyboard":
            if age <= STORYBOARD_STALE_AFTER:
                continue
            job = await storyboard_jobs.find_one({"job_id": job_id}) or {}
            await settle_storyboard(job_id, job.get("status") == "complete" and bool(job.get("storyboard")),
                                    credits=credits, trials=trials, col=col)
            if job.get("status") in _RUNNING:
                await storyboard_jobs.update_one(
                    {"job_id": job_id, "status": {"$in": _RUNNING}},
                    {"$set": {"status": "failed", "error": INTERRUPTED_MESSAGE}})
            counts["storyboards"] += 1
            continue

        if age <= VIDEO_STALE_AFTER:
            continue
        job = await video_jobs.find_one({"job_id": job_id}) or {}
        await settle(job_id, job=job or {"total_scenes": record.get("scenes") or 0, "clips": []},
                     credits=credits, trials=trials, col=col)
        if job.get("status") in _RUNNING:
            await video_jobs.update_one(
                {"job_id": job_id, "status": {"$in": _RUNNING}},
                {"$set": {"status": "failed", "error": INTERRUPTED_MESSAGE}})
        counts["videos"] += 1
    return counts


async def for_job(job_id: str, col=None) -> Optional[dict]:
    """What the customer is shown about a job's charge, or None when billing was not on for it."""
    col = col if col is not None else _collection()
    record = await col.find_one({"job_id": job_id})
    return _public(record) if record and record.get("mode") == "on" else None
