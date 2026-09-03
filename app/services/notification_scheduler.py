"""
Notification Scheduler
Aligned with Notification System PRD V1 — Section 8
+ Subscription Plan Upgrade PRD — Section 8.3

Runs daily batch jobs:
- PRD 8.1: Daily content suggestions (09:00 UTC)
- PRD 8.2: Inactivity reminders (10:00 UTC)
- PRD 8.3: Trial expiry checks (every 6 hours)
- PRD 8.3: Subscription expiry checks (daily at 00:00 UTC)
- WhatsApp daily content push (08:00 UTC / 09:00 WAT)
- Publish scheduled content (every 5 minutes)

Note on publish_scheduled_content: an earlier version of this file ran it
via a separate GitHub Actions workflow instead (publish-scheduled-posts.yml)
and deliberately left it out of this scheduler to avoid double-firing. That
workflow is currently disabled (.yml.txt, not .yml — GitHub only runs the
literal extension) on both dev and prod, so this in-process job is the only
thing actually publishing scheduled posts right now. If that workflow is
ever re-enabled, this job must come back out first, or every post fires
from both places again.
"""
import asyncio
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: BackgroundScheduler = None
_main_loop: asyncio.AbstractEventLoop = None


async def _try_claim_job_run(job_id: str) -> bool:
    """
    Makes a scheduled job's actual work run exactly once per scheduled fire
    time, no matter how many independent schedulers are ticking at once.

    The container runs `uvicorn --workers 4` (see Dockerfile) — four separate
    OS processes, each importing app.main fresh and each calling
    start_notification_scheduler() in its own startup_event(). That's four
    independent APScheduler instances with the identical cron schedule, so
    every job here has always fired 4 times at the same instant (briefly
    more, during a blue/green deploy's old+new task overlap) — this is what
    caused the daily content-idea email to arrive multiple times at once,
    and just as seriously means publish_scheduled_content (every 5 minutes)
    has been attempting to publish the same due post up to 4 times per tick,
    a real risk of duplicate posts landing on a customer's connected
    platform accounts.

    Uses the fire-time-bucketed job id as a Mongo document's _id — the
    collection's unique _id index makes the first insert_one an atomic
    "claim"; every other concurrent caller gets a DuplicateKeyError and
    backs off instead of doing the job's work a second (or fourth) time.
    """
    from app.database import get_db
    from pymongo.errors import DuplicateKeyError

    db = get_db()
    # Minute-precision bucket matches this scheduler's coarsest cron
    # granularity (CronTrigger(minute=...)) — one claim per job per minute.
    lock_id = f"{job_id}:{datetime.utcnow().strftime('%Y-%m-%dT%H:%M')}"
    try:
        await db["scheduled_job_locks"].insert_one({
            "_id": lock_id,
            "job_id": job_id,
            "claimed_at": datetime.utcnow(),
        })
        return True
    except DuplicateKeyError:
        return False


def _run_async(job_id: str, coro_func):
    """Helper to run an async coroutine from a sync APScheduler job.
    Schedules the coroutine on the main event loop so Motor cursors
    (bound to that loop) work correctly. Wraps coro_func with the
    claim check above so only one of the several concurrently-running
    schedulers actually executes it per scheduled fire time.
    """
    async def _guarded():
        if not await _try_claim_job_run(job_id):
            print(f"⏭️  {job_id}: already claimed by another worker for this run, skipping")
            return
        await coro_func()

    if _main_loop is not None and _main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_guarded(), _main_loop)
        try:
            future.result(timeout=300)
        except Exception as e:
            print(f"⚠️ Scheduled job failed: {e}")
    else:
        print("⚠️ Main event loop not available — skipping scheduled job")


def _job_daily_suggestions():
    from app.services.NotificationService import notification_service
    _run_async("daily_suggestions", notification_service.run_daily_suggestions)


def _job_inactivity_check():
    from app.services.NotificationService import notification_service
    _run_async("inactivity_check", notification_service.run_inactivity_check)


def _job_trial_check():
    from app.services.NotificationService import notification_service
    _run_async("trial_check", notification_service.run_trial_check)


def _job_subscription_expiry():
    """Check and expire subscriptions past their end_date"""
    from app.services.SubscriptionService import subscription_service
    _run_async("subscription_expiry", subscription_service.expire_subscriptions)


def _job_whatsapp_daily_push():
    async def _run():
        from app.database import get_db
        from app.agents.social_media_manager.services.whatsapp_flow_service import WhatsAppFlowService
        db = get_db()
        result = await WhatsAppFlowService.send_daily_push(db)
        print(f"📱 WhatsApp daily push complete: {result}")
    _run_async("whatsapp_daily_push", _run)


def _job_publish_scheduled_content():
    async def _run():
        from app.database import get_db
        from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
        db = get_db()
        result = await ApprovalWorkflowService.publish_scheduled_content(db=db)
        published = result.get("published_count", 0)
        errors = result.get("errors", [])
        if published > 0 or errors:
            print(f"📅 Scheduled publish: {published} published, {len(errors)} errors — {errors}")
    _run_async("publish_scheduled_content", _run)


def start_notification_scheduler():
    """Start the APScheduler with all notification batch jobs."""
    global _scheduler, _main_loop

    if _scheduler is not None:
        return

    # Capture main event loop so scheduled jobs can use Motor (which is bound to it)
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        _main_loop = asyncio.get_event_loop()

    _scheduler = BackgroundScheduler(timezone="UTC")

    # misfire_grace_time=None means: if the scheduled time was missed (e.g. server
    # was down or just started after the scheduled hour), DO NOT run the job
    # immediately — wait for the next scheduled occurrence.
    _JOB_DEFAULTS = dict(replace_existing=True, misfire_grace_time=None, coalesce=True)

    # PRD 8.1: Daily content suggestions at 09:00 UTC
    _scheduler.add_job(
        _job_daily_suggestions,
        CronTrigger(hour=9, minute=0),
        id="daily_suggestions",
        **_JOB_DEFAULTS,
    )

    # PRD 8.2: Inactivity reminders at 10:00 UTC
    _scheduler.add_job(
        _job_inactivity_check,
        CronTrigger(hour=10, minute=0),
        id="inactivity_check",
        **_JOB_DEFAULTS,
    )

    # PRD 8.3: Trial expiry checks every 6 hours
    _scheduler.add_job(
        _job_trial_check,
        CronTrigger(hour="*/6", minute=15),
        id="trial_check",
        **_JOB_DEFAULTS,
    )

    # PRD 8.3: Subscription expiry check daily at midnight UTC
    _scheduler.add_job(
        _job_subscription_expiry,
        CronTrigger(hour=0, minute=0),
        id="subscription_expiry",
        **_JOB_DEFAULTS,
    )

    # WhatsApp daily content push at 08:00 UTC (9am WAT)
    _scheduler.add_job(
        _job_whatsapp_daily_push,
        CronTrigger(hour=8, minute=0),
        id="whatsapp_daily_push",
        **_JOB_DEFAULTS,
    )

    # Publish scheduled content every 5 minutes
    _scheduler.add_job(
        _job_publish_scheduled_content,
        CronTrigger(minute="*/5"),
        id="publish_scheduled_content",
        **_JOB_DEFAULTS,
    )

    _scheduler.start()
    print("📅 Notification scheduler started with 6 jobs")


def stop_notification_scheduler():
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
