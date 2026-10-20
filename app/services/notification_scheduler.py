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
- Publish scheduled content (every 5 minutes)

Note on publish_scheduled_content: an earlier version of this file ran it
via a separate GitHub Actions workflow instead (publish-scheduled-posts.yml)
and deliberately left it out of this scheduler to avoid double-firing. That
workflow is currently disabled (.yml.txt, not .yml — GitHub only runs the
literal extension) on both dev and prod, so this in-process job is what
actually publishes scheduled posts. If that workflow is ever re-enabled,
this job must come back out first, or every post fires from both places
again.
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


def _job_publish_confirmations():
    """Posts Outstand accepted but hasn't confirmed live yet: ask what happened,
    mark them published or failed (with the reason), and flag expired logins."""
    async def _run():
        from app.database import get_db
        from app.agents.social_media_manager.services import publish_confirmation
        result = await publish_confirmation.run(get_db())
        if result["published"] or result["failed"] or result["pending"]:
            print(f"🔎 publish_confirmations: {result}")
    _run_async("publish_confirmations", _run)


def _job_connection_health_check():
    """Asks Outstand directly whether each connected account's session is
    still alive, instead of only finding out reactively when a publish
    attempt fails (see connection_health_check.py's module docstring for
    the live-confirmed gap this closes)."""
    async def _run():
        from app.database import get_db
        from app.agents.social_media_manager.services import connection_health_check
        result = await connection_health_check.check_all_connections(get_db())
        print(f"🔌 connection_health_check: {result}")
    _run_async("connection_health_check", _run)


def _brand_alert_job(job_id: str, fn_name: str):
    """Brand Playbook alerts (app/services/brand_alert_jobs.py) — only brands
    that switched the alert on in their Playbook are notified."""
    async def _run():
        from app.database import get_db
        from app.services import brand_alert_jobs
        result = await getattr(brand_alert_jobs, fn_name)(get_db())
        print(f"🔔 {job_id}: {result}")
    _run_async(job_id, _run)


def _job_weekly_reports():
    _brand_alert_job("brand_weekly_reports", "run_weekly_reports")


def _job_engagement_spikes():
    _brand_alert_job("brand_engagement_spikes", "run_engagement_spikes")


def _job_calendar_refill():
    _brand_alert_job("brand_calendar_refill", "run_calendar_refill")


def _job_trending_topics():
    _brand_alert_job("brand_trending_topics", "run_trending_topics")


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


def _job_reconcile_published_posts():
    """Catches an immediately-published ("Publish Now") draft whose platform
    dispatch actually failed after Outstand accepted the submission — the
    publish_scheduled_content cron only ever re-checks "scheduled" drafts, so
    a "published" one that silently failed on Facebook's (or any platform's)
    side had nothing to ever correct it. See
    ApprovalWorkflowService.reconcile_published_posts for the live-reported
    bug this closes."""
    async def _run():
        from app.database import get_db
        from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
        db = get_db()
        result = await ApprovalWorkflowService.reconcile_published_posts(db=db)
        if result.get("checked") or result.get("corrected"):
            print(f"🔁 Published-post reconciliation: {result}")
    _run_async("reconcile_published_posts", _run)


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

    # Brand Playbook alerts — each only reaches brands that switched it on.
    # Weekly report: Mondays 08:00 UTC (09:00 WAT).
    _scheduler.add_job(
        _job_weekly_reports,
        CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="brand_weekly_reports",
        **_JOB_DEFAULTS,
    )
    # Engagement spikes: every 6 hours, after analytics have had time to sync.
    _scheduler.add_job(
        _job_engagement_spikes,
        CronTrigger(hour="*/6", minute=40),
        id="brand_engagement_spikes",
        **_JOB_DEFAULTS,
    )
    # Content calendar running out: daily 09:30 UTC.
    _scheduler.add_job(
        _job_calendar_refill,
        CronTrigger(hour=9, minute=30),
        id="brand_calendar_refill",
        **_JOB_DEFAULTS,
    )
    # Trending topics (real Google Trends only): daily 11:00 UTC.
    _scheduler.add_job(
        _job_trending_topics,
        CronTrigger(hour=11, minute=0),
        id="brand_trending_topics",
        **_JOB_DEFAULTS,
    )

    # Publish scheduled content every 5 minutes — see the module docstring:
    # the GH Actions workflow that used to own this is currently disabled,
    # so this in-process job is what actually publishes scheduled posts.
    _scheduler.add_job(
        _job_publish_scheduled_content,
        CronTrigger(minute="*/5"),
        id="publish_scheduled_content",
        **_JOB_DEFAULTS,
    )

    # reconcile_published_posts is NOT yet registered on an automatic cron —
    # deliberately. It's new, unvalidated against real production data, and
    # on a match it writes a customer-visible status change and fires a real
    # notification. Trigger it manually first via the Admin page ("Reconcile
    # Facebook/Outstand Publishes" in admin_router.py's
    # POST /api/admin/social-media/reconcile-published-posts) and confirm its
    # behavior against real drafts before adding it here on a schedule.

    # Confirm that accepted posts actually reached the network.
    _scheduler.add_job(
        _job_publish_confirmations,
        CronTrigger(minute="*/2"),
        id="publish_confirmations",
        **_JOB_DEFAULTS,
    )

    # Proactively check every Outstand-managed connection's session health —
    # the push side of what get_user_connections already does on pull.
    _scheduler.add_job(
        _job_connection_health_check,
        CronTrigger(hour="*/6", minute=50),
        id="connection_health_check",
        **_JOB_DEFAULTS,
    )

    _scheduler.start()
    print("📅 Notification scheduler started with 15 jobs")


def stop_notification_scheduler():
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
