"""
Notification System Models
Aligned with Notification System PRD V1

Data Model (PRD Section 9):
- Notification object with type, channel, status tracking
- Rate limiting counters
- User notification preferences
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# ==================== PRD Section 3.1: Notification Types ====================

NotificationType = Literal[
    "signup",              # User creates account
    "content_created",     # Content successfully generated
    "content_posted",      # Content successfully published
    "daily_suggestion",    # Content idea for the day
    "inactivity",          # User hasn't posted in X days
    "trial_start",         # Trial activated
    "trial_ending",        # Trial ending soon (24h before)
    "trial_expired",       # Trial ended
]

NotificationChannel = Literal["email", "whatsapp"]
NotificationStatus = Literal["pending", "sent", "failed", "rate_limited"]


# ==================== PRD Section 9: Notification Object ====================

class Notification(BaseModel):
    """
    Core notification record for auditing and deduplication.
    PRD Section 9: Data Model
    """
    notification_id: str = Field(..., description="Unique notification identifier")
    user_id: str = Field(..., description="Target user")
    type: NotificationType = Field(..., description="Notification event type")
    channel: NotificationChannel = Field(default="email", description="Delivery channel")
    status: NotificationStatus = Field(default="pending", description="Delivery status")
    subject: str = Field(default="", description="Email subject line")
    metadata: dict = Field(default_factory=dict, description="Extra context (content preview, CTA links)")
    retry_count: int = Field(default=0, description="PRD Section 10: Retry attempts")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None
    error: Optional[str] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# ==================== PRD Section 8: User Fields ====================

class UserNotificationFields(BaseModel):
    """Fields to add/update on user documents for notification scheduling"""
    last_active_at: Optional[datetime] = None
    email: str = ""
    phone_number: Optional[str] = None  # For WhatsApp
    notification_opt_out: bool = False
