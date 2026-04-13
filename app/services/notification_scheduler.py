"""
Notification Scheduler
Aligned with Notification System PRD V1 — Section 8

Runs daily batch jobs:
- PRD 8.1: Daily content suggestions (09:00 UTC)
- PRD 8.2: Inactivity reminders (10:00 UTC)
- PRD 8.3: Trial expiry checks (every 6 hours)
"""
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: BackgroundScheduler = None


def _run_async(coro_func):
    """Helper to run an async coroutine from a sync APScheduler job."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(coro_func())
        else:
            loop.run_until_complete(coro_func())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro_func())


def _job_daily_suggestions():
    from app.services.NotificationService import notification_service
    _run_async(notification_service.run_daily_suggestions)


def _job_inactivity_check():
    from app.services.NotificationService import notification_service
    _run_async(notification_service.run_inactivity_check)


def _job_trial_check():
    from app.services.NotificationService import notification_service
    _run_async(notification_service.run_trial_check)


def start_notification_scheduler():
    """Start the APScheduler with all notification batch jobs."""
    global _scheduler

    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    # PRD 8.1: Daily content suggestions at 09:00 UTC
    _scheduler.add_job(
        _job_daily_suggestions,
        CronTrigger(hour=9, minute=0),
        id="daily_suggestions",
        replace_existing=True,
    )

    # PRD 8.2: Inactivity reminders at 10:00 UTC
    _scheduler.add_job(
        _job_inactivity_check,
        CronTrigger(hour=10, minute=0),
        id="inactivity_check",
        replace_existing=True,
    )

    # PRD 8.3: Trial expiry checks every 6 hours
    _scheduler.add_job(
        _job_trial_check,
        CronTrigger(hour="*/6", minute=15),
        id="trial_check",
        replace_existing=True,
    )

    _scheduler.start()
    print("📅 Notification scheduler started with 3 jobs")


def stop_notification_scheduler():
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
