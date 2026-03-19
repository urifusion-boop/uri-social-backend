from pydantic import BaseModel
from typing import List, Optional


class ChatMessage(BaseModel):
    role: str = "user"
    content: str


class ChatModel(BaseModel):
    model: str = "gpt-4o-mini"
    messages: List[ChatMessage]
    temperature: float = 0.5


class EmbeddingModel(BaseModel):
    model: str = "text-embedding-ada-002"
    input: str


class ImageModel(BaseModel):
    model: str = "image-dalle"
    prompt: str
    n: int = 1
    size: str = "1024x1024"


class AudioModel(BaseModel):
    url: str


class PlainText(BaseModel):
    text: str


class SentimentResponse(BaseModel):
    score: float
    magnitude: float
    sentiment: str  # "positive", "neutral", or "negative"


class NextStepAction(BaseModel):
    """Single actionable next step for a lead"""
    step_id: str
    action: str
    reasoning: str
    priority: str  # "high", "medium", "low"
    confidence: float  # 0.0 to 1.0
    platform: Optional[str] = None
    completed: bool = False


class AINextStepsResponse(BaseModel):
    """AI-generated next steps for a lead based on user's goal"""
    steps: List[NextStepAction]
    summary: str
    based_on_goal: str
