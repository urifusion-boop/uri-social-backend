"""
Uri Market Intelligence — keyword suggestion (PRD §9: "Uri suggests
keywords, related phrases and exclusions. The owner reviews them before
starting collection.").

Replaces the previous placeholder — splitting the question into words over
3 characters — with a real suggestion the owner can edit before a topic is
created. Never used to auto-create a topic; the frontend shows these as
editable chips, matching "the owner reviews them before starting collection."
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.services.AIService import AIService

PROMPT_VERSION = "mi-keywords-v1"
MODEL_NAME = "gpt-4o-mini"


class _KeywordLLMOutput(BaseModel):
    keywords: list[str] = Field(description="3-8 concrete search terms/phrases likely to surface real evidence for this question.")
    excluded_keywords: list[str] = Field(
        default_factory=list,
        description="Terms that would cause obviously irrelevant matches (e.g. an unrelated meaning of a keyword) — leave empty if nothing obvious applies.",
    )


SYSTEM_PROMPT = """You suggest search keywords for a market-research topic. The business owner will review and edit
every suggestion before anything runs — never invent facts about their business, only work from the question given.

Rules:
- keywords: 3-8 concrete terms or short phrases a real customer conversation about this question would actually use —
  not generic restatements of the question itself.
- excluded_keywords: terms that would cause obviously irrelevant matches (e.g. a homonym with an unrelated common
  meaning). Leave this empty if nothing obvious applies — do not invent exclusions just to fill the list.
- Never suggest a competitor name, brand name, or fact not present in the question itself."""


async def suggest_keywords(question: str) -> Optional[_KeywordLLMOutput]:
    """Returns None on any failure — callers must fall back to the question
    text itself (e.g. a simple word split), never fabricate keywords."""
    ai_request = AIService.build_ai_model(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Question: {question}\n\nSuggest keywords."},
        ],
        temperature=0.2,
    )
    try:
        completion = await AIService.structured_chat_completion(ai_request, response_model=_KeywordLLMOutput)
    except Exception as e:
        print(f"[MI][keywords] AI call raised: {e}")
        return None

    if isinstance(completion, dict) and "error" in completion:
        print(f"[MI][keywords] AI call failed: {completion['error']}")
        return None

    choice = completion.choices[0]
    if getattr(choice.message, "refusal", None):
        print(f"[MI][keywords] Model refused: {choice.message.refusal}")
        return None

    return choice.message.parsed
