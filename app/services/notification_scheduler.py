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
- Jane + Ads mid-flight monitoring (every 4 hours) — campaign roadmap Tier 4
- Jane + Ads ad-spend billing (hourly) — recoup Meta spend × markup from prepaid wallets

Note: publish_scheduled_content is intentionally NOT in this scheduler.
It is triggered every 5 minutes by the GitHub Actions workflow
(.github/workflows/publish-scheduled-posts.yml) via POST /social-media/publish-scheduled.
Running it here AND in GH Actions would create duplicate publish attempts.
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
    and just as seriously means jane_ads_billing (hourly wallet charges) has
    been quadruple-charging customer wallets on every run, silently, since
    nothing about that job's output looks wrong from a single log line.

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


def _job_jane_ads_monitoring():
    """Jane + Ads mid-flight monitoring (campaign roadmap Tier 4) — flags
    underperforming campaigns and announces finished ones."""
    async def _run():
        from app.database import get_db
        from app.agents.jane_ads.monitoring import check_active_campaigns
        db = get_db()
        result = await check_active_campaigns(db)
        print(f"📊 Jane Ads monitoring: {result}")
    _run_async("jane_ads_monitoring", _run)


def _job_jane_ads_token_health():
    """Jane + Ads per-brand connection token health (Per-Brand Page Connection
    plan §8) — daily, since a token dying mid-flight is rare but silent otherwise:
    pauses that brand's running campaigns and notifies the owner once."""
    async def _run():
        from app.database import get_db
        from app.agents.jane_ads.ads_connection import run_token_health_check
        db = get_db()
        result = await run_token_health_check(db)
        print(f"🔑 Jane Ads token health: {result}")
    _run_async("jane_ads_token_health", _run)


def _job_jane_ads_billing():
    """Jane + Ads ad-spend billing — recoups real Meta spend (× markup) from each
    customer's prepaid wallet and pauses any campaign whose wallet can no longer
    cover it, so URI never fronts money it can't recoup."""
    async def _run():
        from app.database import get_db
        from app.agents.jane_ads.billing import reconcile_ad_spend_charges
        db = get_db()
        result = await reconcile_ad_spend_charges(db)
        print(f"💳 Jane Ads billing: {result}")
    _run_async("jane_ads_billing", _run)


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

    # Jane + Ads mid-flight monitoring every 4 hours — ad campaigns move faster
    # than subscription/trial checks, so this runs more often than those.
    _scheduler.add_job(
        _job_jane_ads_monitoring,
        CronTrigger(hour="*/4", minute=30),
        id="jane_ads_monitoring",
        **_JOB_DEFAULTS,
    )

    # Jane + Ads ad-spend billing hourly — recoups real Meta spend from the
    # customer's prepaid wallet and pauses a campaign the moment its wallet can't
    # cover more. Runs more often than monitoring to keep unrecouped spend small.
    _scheduler.add_job(
        _job_jane_ads_billing,
        CronTrigger(minute=0),
        id="jane_ads_billing",
        **_JOB_DEFAULTS,
    )

    # Jane + Ads per-brand ads connection token health, daily at 06:00 UTC — a
    # dead token doesn't stop a launched campaign from delivering wrong (or
    # spending), so this is a proactive pause+notify, not just a read-time check.
    _scheduler.add_job(
        _job_jane_ads_token_health,
        CronTrigger(hour=6, minute=0),
        id="jane_ads_token_health",
        **_JOB_DEFAULTS,
    )

    _scheduler.start()
    print("📅 Notification scheduler started with 8 jobs")


def stop_notification_scheduler():
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
