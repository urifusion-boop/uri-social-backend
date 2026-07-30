"""
Jane's First Message - Data Models
PRD: URI-Social-Jane-First-Message-PRD.pdf

Models for tracking Jane's personalized first message to new users.
Goal: Turn passive signups into active users with one contextual offer.
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# Hook types (PRD Section 5)
HookType = Literal["seasonal", "trending", "evergreen"]

# Message status tracking
MessageStatus = Literal["shown", "accepted", "declined", "ignored"]


class JaneFirstMessage(BaseModel):
    """
    Jane's first message to a new user.
    PRD Section 4: What the Message Must Contain

    Structure:
    1. Prove it was listening (references their business/industry)
    2. Genuinely specific, timely hook (seasonal/calendar backbone)
    3. Low-effort offer
    4. One clear next step
    """
    message_id: str = Field(..., description="Unique message identifier")
    user_id: str = Field(..., description="Target user ID")

    # Message content
    message_text: str = Field(..., description="Full message shown to user")
    proof_listening: str = Field(..., description="Business/industry reference")
    timely_hook: str = Field(..., description="Specific seasonal/trending angle")
    offer_text: str = Field(..., description="Low-effort offer")

    # Hook metadata
    hook_type: HookType = Field(..., description="Type of hook used")
    hook_source: str = Field(..., description="Where hook came from (e.g., 'Nigerian Calendar - Valentine's')")

    # Generation context
    seed_content: str = Field(..., description="Content seed for when user accepts")
    platforms_suggested: list[str] = Field(default_factory=list, description="Suggested platforms")

    # Tracking
    status: MessageStatus = Field(default="shown", description="User response status")
    draft_id: Optional[str] = Field(default=None, description="Generated draft ID if accepted")

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    shown_at: Optional[datetime] = None
    responded_at: Optional[datetime] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class JaneMessageResponse(BaseModel):
    """API response for fetching Jane's first message"""
    message_id: str
    message: str
    hook: str
    seed_content: str
    platforms_suggested: list[str]


class AcceptMessageRequest(BaseModel):
    """Request to accept Jane's first message and generate content"""
    message_id: str
    platforms: Optional[list[str]] = None  # Override suggested platforms


class UserFirstMessageFields(BaseModel):
    """
    Fields to track on user document.
    PRD Section 9.1: Measure publishes-after-message
    """
    first_message_shown: bool = False
    first_message_accepted: bool = False
    first_message_generated_at: Optional[datetime] = None
    first_content_published: bool = False  # Key metric!
    first_publish_timestamp: Optional[datetime] = None
