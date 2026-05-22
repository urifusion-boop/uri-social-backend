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
"""
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: BackgroundScheduler = None
_main_loop: asyncio.AbstractEventLoop = None


def _run_async(coro_func):
    """Helper to run an async coroutine from a sync APScheduler job.
    Schedules the coroutine on the main event loop so Motor cursors
    (bound to that loop) work correctly.
    """
    if _main_loop is not None and _main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro_func(), _main_loop)
        try:
            future.result(timeout=300)
        except Exception as e:
            print(f"⚠️ Scheduled job failed: {e}")
    else:
        print("⚠️ Main event loop not available — skipping scheduled job")


def _job_daily_suggestions():
    """Daily suggestions job with duplicate prevention on container restart."""
    from app.services.NotificationService import notification_service
    from datetime import datetime, timedelta
    from app.database import get_db

    # Check if we already sent suggestions today (prevents duplicate on restart)
    async def check_and_run():
        db = get_db()
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Check if any daily_suggestion was sent today
        recent_suggestion = await db["notifications"].find_one({
            "type": "daily_suggestion",
            "status": "sent",
            "sent_at": {"$gte": today_start}
        })

        if recent_suggestion:
            print("⏭️ Daily suggestions already sent today, skipping to prevent duplicates")
            return

        # Safe to send
        await notification_service.run_daily_suggestions()

    _run_async(check_and_run)


def _job_inactivity_check():
    from app.services.NotificationService import notification_service
    _run_async(notification_service.run_inactivity_check)


def _job_trial_check():
    from app.services.NotificationService import notification_service
    _run_async(notification_service.run_trial_check)


def _job_subscription_expiry():
    """Check and expire subscriptions past their end_date"""
    from app.services.SubscriptionService import subscription_service
    _run_async(subscription_service.expire_subscriptions)


def _job_whatsapp_daily_push():
    async def _run():
        from app.database import get_db
        from app.agents.social_media_manager.services.whatsapp_flow_service import WhatsAppFlowService
        db = get_db()
        result = await WhatsAppFlowService.send_daily_push(db)
        print(f"📱 WhatsApp daily push complete: {result}")
    _run_async(_run)


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
    _run_async(_run)


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
