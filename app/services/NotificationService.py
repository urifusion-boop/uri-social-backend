"""
Notification Service
Aligned with Notification System PRD V1 — Section 5

Responsibilities (PRD 5.1):
- Listen to system events
- Trigger notifications
- Route to correct channel
- Rate limiting (PRD 11)
- Deduplication
- Async processing (PRD 5.3)

Event mapping (PRD 4):
- user_signed_up → welcome email + trial start email
- content_created → content ready email
- content_posted → content posted email
- user_inactive → inactivity reminder email
- trial_ending → trial ending email
- trial_expired → trial expired email
"""
import uuid
import asyncio
from typing import Optional
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.core.config import settings
from app.services.EmailService import email_service
from app.domain.models.notification_models import (
    Notification,
    NotificationType,
    NotificationChannel,
)

# PRD 11: Rate limiting — max emails per user per day
MAX_EMAILS_PER_DAY = 3
MAX_WHATSAPP_PER_DAY = 2
INACTIVITY_THRESHOLD_DAYS = 3  # PRD 4.5


class NotificationService:
    """
    Centralized notification system.
    PRD 5.1: Dedicated module that listens to events and routes to channels.
    """

    def __init__(self):
        self._db: Optional[AsyncIOMotorDatabase] = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            self._db = get_db()
        return self._db

    @property
    def notifications_collection(self):
        return self.db["notifications"]

    @property
    def users_collection(self):
        return self.db["users"]

    # ==================== PRD 11: Rate Limiting ====================

    async def _check_rate_limit(
        self, user_id: str, channel: NotificationChannel = "email"
    ) -> bool:
        """Check if user has exceeded daily notification limit."""
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        limit = MAX_EMAILS_PER_DAY if channel == "email" else MAX_WHATSAPP_PER_DAY

        count = await self.notifications_collection.count_documents({
            "user_id": user_id,
            "channel": channel,
            "status": "sent",
            "sent_at": {"$gte": today_start},
        })

        return count < limit

    # ==================== Smart Reminder Deduplication ====================

    async def _should_send_notification(
        self,
        user_id: str,
        notification_type: NotificationType,
        reminder_config: dict = None
    ) -> bool:
        """
        Check if notification should be sent based on smart reminder rules.

        Args:
            user_id: User to notify
            notification_type: Type of notification
            reminder_config: Optional config with:
                - first_only: Send only once, never remind (default: False)
                - reminder_days: Days between reminders (default: 3)
                - max_reminders: Max number of reminders (default: 3)

        Returns:
            True if should send, False if already sent recently
        """
        if reminder_config is None:
            reminder_config = {}

        first_only = reminder_config.get('first_only', False)
        reminder_days = reminder_config.get('reminder_days', 3)
        max_reminders = reminder_config.get('max_reminders', 3)

        # Find last notification of this type for this user
        last_notification = await self.notifications_collection.find_one(
            {
                "user_id": user_id,
                "type": notification_type,
                "status": "sent"
            },
            sort=[("created_at", -1)]
        )

        # No previous notification - send it
        if not last_notification:
            return True

        # If first_only mode, don't send again
        if first_only:
            return False

        # Check how many times we've sent this notification
        total_sent = await self.notifications_collection.count_documents({
            "user_id": user_id,
            "type": notification_type,
            "status": "sent"
        })

        # If reached max reminders, stop
        if total_sent >= max_reminders + 1:  # +1 for initial notification
            return False

        # Check if enough time has passed for a reminder
        last_sent_at = last_notification.get("created_at")
        if last_sent_at:
            days_since = (datetime.utcnow() - last_sent_at).days
            if days_since >= reminder_days:
                return True

        # Too soon for a reminder
        return False

    # ==================== PRD 9: Log Notification ====================

    async def _log_notification(
        self,
        user_id: str,
        notification_type: NotificationType,
        channel: NotificationChannel,
        subject: str,
        status: str,
        metadata: dict = None,
        error: str = None,
    ) -> str:
        """Log notification to DB for auditing and deduplication."""
        notification_id = str(uuid.uuid4())
        now = datetime.utcnow()

        doc = Notification(
            notification_id=notification_id,
            user_id=user_id,
            type=notification_type,
            channel=channel,
            status=status,
            subject=subject,
            metadata=metadata or {},
            sent_at=now if status == "sent" else None,
            created_at=now,
            error=error,
        ).dict()

        await self.notifications_collection.insert_one(doc)
        return notification_id

    # ==================== Duplicate Prevention ====================

    async def _was_recently_sent(
        self, user_id: str, notification_type: NotificationType, hours: int = 24
    ) -> bool:
        """Prevent duplicate notifications within a time window."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        existing = await self.notifications_collection.find_one({
            "user_id": user_id,
            "type": notification_type,
            "status": "sent",
            "sent_at": {"$gte": cutoff},
        })
        return existing is not None

    # ==================== Helper: Get user info ====================

    async def _get_user(self, user_id: str) -> Optional[dict]:
        return await self.users_collection.find_one({"userId": user_id})

    # ==================== PRD 4.1: Signup Notification ====================

    async def notify_signup(
        self,
        user_id: str,
        email: str,
        first_name: str = "",
        trial_days: int = 0,
        trial_credits: int = 0,
    ):
        """
        PRD 4.1: Welcome email on signup
        PRD 4.6: Trial start email (if applicable)
        """
        if not await self._check_rate_limit(user_id):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = first_name or email.split("@")[0]

        # Welcome email
        subject = "Welcome to URI Social! 🎉"
        success = await email_service.send_email(
            to_email=email,
            subject=subject,
            template_name="welcome",
            template_vars={
                "user_name": user_name,
                "trial_days": trial_days,
                "trial_credits": trial_credits,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="signup",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"trial_days": trial_days, "trial_credits": trial_credits},
            error="Email delivery failed" if not success else None,
        )

        # Update last_active_at (PRD 8)
        await self.users_collection.update_one(
            {"userId": user_id},
            {"$set": {"last_active_at": datetime.utcnow()}},
        )

    # ==================== Admin: New Signup Alert ====================

    async def notify_admin_new_signup(
        self,
        email: str,
        first_name: str = "",
        last_name: str = "",
        auth_provider: str = "email",
    ):
        """Send admin notification email when a new user signs up."""
        admin_email = settings.ADMIN_NOTIFICATION_EMAIL
        if not admin_email:
            print("⚠️ ADMIN_NOTIFICATION_EMAIL not set — skipping admin signup alert")
            return

        now = datetime.utcnow()
        full_name = f"{first_name} {last_name}".strip() or "N/A"
        subject = f"New Signup: {full_name} ({email})"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
            <h2 style="color:#6366f1;">New User Signup</h2>
            <table style="width:100%;border-collapse:collapse;">
                <tr><td style="padding:8px;font-weight:bold;">Email</td><td style="padding:8px;">{email}</td></tr>
                <tr><td style="padding:8px;font-weight:bold;">Name</td><td style="padding:8px;">{full_name}</td></tr>
                <tr><td style="padding:8px;font-weight:bold;">Auth</td><td style="padding:8px;">{auth_provider}</td></tr>
                <tr><td style="padding:8px;font-weight:bold;">Time (UTC)</td><td style="padding:8px;">{now.strftime("%Y-%m-%d %H:%M:%S")}</td></tr>
            </table>
        </div>
        """

        success = await email_service.send_raw_email(
            to_email=admin_email,
            subject=subject,
            html_body=html,
        )
        if success:
            print(f"📧 Admin notified of new signup: {email}")
        else:
            print(f"⚠️ Failed to send admin signup alert for {email}")

    # ==================== PRD 4.2: Content Created Notification ====================

    async def notify_content_created(
        self,
        user_id: str,
        content_preview: str = "",
        platforms: str = "",
        campaign_id: str = "",
    ):
        """PRD 4.2: Email when content generation completes."""
        user = await self._get_user(user_id)
        if not user or user.get("notification_opt_out"):
            return

        if not await self._check_rate_limit(user_id):
            return

        # Don't send if we already sent one today for content_created
        if await self._was_recently_sent(user_id, "content_created", hours=4):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = user.get("first_name") or user.get("email", "").split("@")[0]

        subject = "Your Content is Ready! ✨"
        success = await email_service.send_email(
            to_email=user["email"],
            subject=subject,
            template_name="content_created",
            template_vars={
                "user_name": user_name,
                "content_preview": content_preview,
                "platforms": platforms,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="content_created",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"campaign_id": campaign_id, "platforms": platforms},
        ,
            error="Email delivery failed" if not success else None,
        )

        # Update last_active_at
        await self.users_collection.update_one(
            {"userId": user_id},
            {"$set": {"last_active_at": datetime.utcnow()}},
        )

    # ==================== PRD 4.3: Content Posted Notification ====================

    async def notify_content_posted(
        self,
        user_id: str,
        platform: str = "",
        content_preview: str = "",
        campaign_id: str = "",
    ):
        """PRD 4.3: Email when content is successfully published."""
        user = await self._get_user(user_id)
        if not user or user.get("notification_opt_out"):
            return

        if not await self._check_rate_limit(user_id):
            return

        if await self._was_recently_sent(user_id, "content_posted", hours=2):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = user.get("first_name") or user.get("email", "").split("@")[0]

        subject = f"Content Published on {platform}! 🚀"
        success = await email_service.send_email(
            to_email=user["email"],
            subject=subject,
            template_name="content_posted",
            template_vars={
                "user_name": user_name,
                "platform": platform,
                "content_preview": content_preview,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="content_posted",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"platform": platform, "campaign_id": campaign_id},
        ,
            error="Email delivery failed" if not success else None,
        )

    # ==================== PRD 4.4: Daily Suggestion ====================

    async def notify_daily_suggestion(
        self,
        user_id: str,
        suggestion: str = "",
        topic: str = "",
    ):
        """PRD 4.4: Daily content suggestion email."""
        user = await self._get_user(user_id)
        if not user or user.get("notification_opt_out"):
            return

        if not await self._check_rate_limit(user_id):
            return

        if await self._was_recently_sent(user_id, "daily_suggestion", hours=20):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = user.get("first_name") or user.get("email", "").split("@")[0]

        subject = "Today's Content Idea 💡"
        success = await email_service.send_email(
            to_email=user["email"],
            subject=subject,
            template_name="daily_suggestion",
            template_vars={
                "user_name": user_name,
                "suggestion": suggestion,
                "topic": topic,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="daily_suggestion",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"suggestion": suggestion, "topic": topic},
        ,
            error="Email delivery failed" if not success else None,
        )

    # ==================== PRD 4.5: Inactivity Notification ====================

    async def notify_inactivity(
        self,
        user_id: str,
        days_inactive: int,
        suggestion: str = "",
        credits_remaining: int = None,
    ):
        """PRD 4.5: Reminder when user hasn't posted in X days."""
        user = await self._get_user(user_id)
        if not user or user.get("notification_opt_out"):
            return

        if not await self._check_rate_limit(user_id):
            return

        if await self._was_recently_sent(user_id, "inactivity", hours=48):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = user.get("first_name") or user.get("email", "").split("@")[0]

        subject = "We Miss You! Your Audience is Waiting 👋"
        success = await email_service.send_email(
            to_email=user["email"],
            subject=subject,
            template_name="inactivity",
            template_vars={
                "user_name": user_name,
                "days_inactive": days_inactive,
                "suggestion": suggestion,
                "credits_remaining": credits_remaining,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="inactivity",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"days_inactive": days_inactive},
        ,
            error="Email delivery failed" if not success else None,
        )

    # ==================== PRD 4.6: Trial Notifications ====================

    async def notify_trial_start(
        self,
        user_id: str,
        email: str,
        first_name: str = "",
        trial_days: int = 3,
        trial_credits: int = 10,
    ):
        """PRD 4.6: Trial started notification (sent alongside welcome)."""
        # Trial start is handled within welcome email template
        # This is only called separately if the trial is activated independently
        if await self._was_recently_sent(user_id, "trial_start", hours=24):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = first_name or email.split("@")[0]

        subject = "Your Free Trial Has Started! 🎯"
        success = await email_service.send_email(
            to_email=email,
            subject=subject,
            template_name="trial_start",
            template_vars={
                "user_name": user_name,
                "trial_days": trial_days,
                "trial_credits": trial_credits,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="trial_start",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"trial_days": trial_days, "trial_credits": trial_credits},
        ,
            error="Email delivery failed" if not success else None,
        )

    async def notify_trial_ending(
        self,
        user_id: str,
        credits_remaining: int = 0,
    ):
        """PRD 4.6: Trial ending soon (24h before expiry)."""
        user = await self._get_user(user_id)
        if not user or user.get("notification_opt_out"):
            return

        if await self._was_recently_sent(user_id, "trial_ending", hours=24):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = user.get("first_name") or user.get("email", "").split("@")[0]

        subject = "Your Trial Ends Soon ⏳"
        success = await email_service.send_email(
            to_email=user["email"],
            subject=subject,
            template_name="trial_ending",
            template_vars={
                "user_name": user_name,
                "credits_remaining": credits_remaining,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="trial_ending",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={"credits_remaining": credits_remaining},
        ,
            error="Email delivery failed" if not success else None,
        )

    async def notify_trial_expired(self, user_id: str):
        """PRD 4.6: Trial expired — upgrade prompt."""
        user = await self._get_user(user_id)
        if not user or user.get("notification_opt_out"):
            return

        if await self._was_recently_sent(user_id, "trial_expired", hours=48):
            return

        app_url = settings.WEB_APP_URL or "https://app.urisocial.com"
        user_name = user.get("first_name") or user.get("email", "").split("@")[0]

        subject = "Your Free Trial Has Ended"
        success = await email_service.send_email(
            to_email=user["email"],
            subject=subject,
            template_name="trial_expired",
            template_vars={
                "user_name": user_name,
                "app_url": app_url,
                "year": str(datetime.utcnow().year),
            },
        )

        await self._log_notification(
            user_id=user_id,
            notification_type="trial_expired",
            channel="email",
            subject=subject,
            status="sent" if success else "failed",
            metadata={},
            error="Email delivery failed" if not success else None,
        )

    # ==================== PRD 8: Activity Tracking ====================

    async def update_user_activity(self, user_id: str):
        """Update last_active_at timestamp for inactivity tracking."""
        await self.users_collection.update_one(
            {"userId": user_id},
            {"$set": {"last_active_at": datetime.utcnow()}},
        )

    # ==================== Batch Jobs (PRD 8) ====================

    async def run_inactivity_check(self):
        """
        PRD 8.2: Check all users for inactivity and send reminders.
        Called by the scheduler daily at 10:00 AM UTC.
        Now with smart reminders: 3 days, 7 days, 14 days.
        """
        cutoff = datetime.utcnow() - timedelta(days=INACTIVITY_THRESHOLD_DAYS)

        # Find users who haven't been active
        inactive_users = self.users_collection.find({
            "last_active_at": {"$lt": cutoff, "$exists": True},
            "notification_opt_out": {"$ne": True},
        })

        count = 0
        skipped = 0
        async for user in inactive_users:
            user_id = user.get("userId")
            if not user_id:
                continue

            last_active = user.get("last_active_at")
            days_inactive = (datetime.utcnow() - last_active).days if last_active else INACTIVITY_THRESHOLD_DAYS

            # Smart reminder: Send at 3 days, 7 days, 14 days
            # Determine which reminder interval to use based on days_inactive
            if days_inactive >= 14:
                reminder_interval = 7  # After 14 days, remind every 7 days
            elif days_inactive >= 7:
                reminder_interval = 7  # Second reminder at 7 days
            else:
                reminder_interval = 3  # First reminder at 3 days

            # Check if we should send based on smart reminders
            should_send = await self._should_send_notification(
                user_id=user_id,
                notification_type="inactivity",
                reminder_config={
                    "reminder_days": reminder_interval,
                    "max_reminders": 3  # Max 3 inactivity reminders (at 3d, 7d, 14d)
                }
            )

            if not should_send:
                skipped += 1
                continue

            try:
                await self.notify_inactivity(
                    user_id=user_id,
                    days_inactive=days_inactive,
                )
                count += 1
            except Exception as e:
                print(f"⚠️ Inactivity notification failed for {user_id}: {e}")

        print(f"📬 Inactivity check: {count} sent, {skipped} skipped (too soon for reminder)")
        return count

    async def run_trial_check(self):
        """
        PRD 8.3: Check all trials for expiry and send notifications.
        Called by the scheduler every 6 hours.
        Now with smart deduplication to prevent spam.
        """
        now = datetime.utcnow()
        trials_collection = self.db["user_trials"]

        # Trial ending (within 24 hours) - Send ONCE only
        ending_cutoff = now + timedelta(hours=24)
        ending_trials = trials_collection.find({
            "trial_end_date": {"$gt": now, "$lte": ending_cutoff},
            "credits_remaining": {"$gt": 0},
        })

        ending_count = 0
        ending_skipped = 0
        async for trial in ending_trials:
            try:
                user_id = trial["user_id"]

                # Check if we should send (first_only: only send once, never remind)
                should_send = await self._should_send_notification(
                    user_id=user_id,
                    notification_type="trial_ending",
                    reminder_config={"first_only": True}  # Never remind for trial ending
                )

                if should_send:
                    await self.notify_trial_ending(
                        user_id=user_id,
                        credits_remaining=trial.get("credits_remaining", 0),
                    )
                    ending_count += 1
                else:
                    ending_skipped += 1
            except Exception as e:
                print(f"⚠️ Trial ending notification failed: {e}")

        # Trial expired - Send with smart reminders (every 3 days, max 3 times)
        expired_cutoff = now - timedelta(days=10)  # Check last 10 days
        expired_trials = trials_collection.find({
            "trial_end_date": {"$gt": expired_cutoff, "$lte": now},
        })

        expired_count = 0
        expired_skipped = 0
        async for trial in expired_trials:
            try:
                user_id = trial["user_id"]

                # Check if we should send (remind every 3 days, max 3 reminders)
                should_send = await self._should_send_notification(
                    user_id=user_id,
                    notification_type="trial_expired",
                    reminder_config={
                        "reminder_days": 3,  # Remind every 3 days
                        "max_reminders": 3   # Stop after 3 reminders (9 days total)
                    }
                )

                if should_send:
                    await self.notify_trial_expired(user_id=user_id)
                    expired_count += 1
                else:
                    expired_skipped += 1
            except Exception as e:
                print(f"⚠️ Trial expired notification failed: {e}")

        print(f"📬 Trial check complete:")
        print(f"   Trial ending: {ending_count} sent, {ending_skipped} skipped (already notified)")
        print(f"   Trial expired: {expired_count} sent, {expired_skipped} skipped (too soon for reminder)")
        return {"ending": ending_count, "expired": expired_count}

    async def run_daily_suggestions(self):
        """
        PRD 8.1: Send daily content suggestions to active users.
        Called by the scheduler daily at 9:00 AM UTC.
        Now with deduplication to ensure max 1 suggestion per day.
        """
        # Find active users who have been active in last 14 days (engaged users)
        cutoff = datetime.utcnow() - timedelta(days=14)

        active_users = self.users_collection.find({
            "last_active_at": {"$gte": cutoff},
            "notification_opt_out": {"$ne": True},
        })

        # Default suggestions (in production, use AI to personalize)
        default_suggestions = [
            "Share a behind-the-scenes look at your process today.",
            "Post a quick tip that your audience would find valuable.",
            "Tell your audience about a lesson you've learned recently.",
            "Share a customer success story or testimonial.",
            "Create a poll or question to boost engagement.",
            "Post about an industry trend that matters to your audience.",
            "Share your take on a recent development in your field.",
        ]

        count = 0
        skipped = 0
        async for user in active_users:
            user_id = user.get("userId")
            if not user_id:
                continue

            # Check if we already sent a suggestion today (1 day interval)
            should_send = await self._should_send_notification(
                user_id=user_id,
                notification_type="daily_suggestion",
                reminder_config={
                    "reminder_days": 1,  # Daily reminders
                    "max_reminders": 365  # Essentially unlimited for daily suggestions
                }
            )

            if not should_send:
                skipped += 1
                continue

            import random
            suggestion = random.choice(default_suggestions)

            try:
                await self.notify_daily_suggestion(
                    user_id=user_id,
                    suggestion=suggestion,
                )
                count += 1
            except Exception as e:
                print(f"⚠️ Daily suggestion failed for {user_id}: {e}")

        print(f"📬 Daily suggestions: {count} sent, {skipped} skipped (already sent today)")
        return count


# Singleton
notification_service = NotificationService()
