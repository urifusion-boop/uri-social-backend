"""
Notification System Models
Aligned with Notification System PRD V1

Data Model (PRD Section 9):
- Notification object with type, channel, status tracking
- Rate limiting counters
- User notification preferences
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal, Union
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
    "payment_success",     # Payment completed successfully
]

NotificationChannel = Literal["email", "whatsapp"]
NotificationStatus = Literal["pending", "sent", "failed", "rate_limited"]


# ==================== Metadata Models ====================

class SignupMetadata(BaseModel):
    """Metadata for signup notifications"""
    trial_days: Optional[int] = None
    trial_credits: Optional[int] = None

class ContentCreatedMetadata(BaseModel):
    """Metadata for content_created notifications"""
    campaign_id: Optional[str] = None
    platforms: Optional[str] = None
    message: Optional[str] = None  # Full notification message

class ContentPostedMetadata(BaseModel):
    """Metadata for content_posted notifications"""
    platform: Optional[str] = None
    campaign_id: Optional[str] = None
    message: Optional[str] = None

class DailySuggestionMetadata(BaseModel):
    """Metadata for daily_suggestion notifications"""
    suggestion: Optional[str] = None
    topic: Optional[str] = None
    message: Optional[str] = None

class InactivityMetadata(BaseModel):
    """Metadata for inactivity notifications"""
    days_inactive: Optional[int] = None
    message: Optional[str] = None

class TrialMetadata(BaseModel):
    """Metadata for trial notifications"""
    trial_days: Optional[int] = None
    trial_credits: Optional[int] = None
    credits_remaining: Optional[int] = None
    message: Optional[str] = None

class PaymentSuccessMetadata(BaseModel):
    """Metadata for payment_success notifications"""
    amount: Optional[float] = None
    currency: Optional[str] = None
    subscription_tier: Optional[str] = None
    credits_added: Optional[int] = None
    transaction_ref: Optional[str] = None
    message: Optional[str] = None


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
    metadata: Union[SignupMetadata, ContentCreatedMetadata, ContentPostedMetadata, DailySuggestionMetadata, InactivityMetadata, TrialMetadata, PaymentSuccessMetadata, dict] = Field(default_factory=dict, description="Extra context (content preview, CTA links)")
    retry_count: int = Field(default=0, description="PRD Section 10: Retry attempts")
    read: bool = Field(default=False, description="Whether notification has been read")
    read_at: Optional[datetime] = None
    archived: bool = Field(default=False, description="Whether notification is archived")
    archived_at: Optional[datetime] = None
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
