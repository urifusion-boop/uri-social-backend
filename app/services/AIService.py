from openai import OpenAI
from app.core.config import settings
from app.domain.models.chat_model import (
    ChatModel,
    EmbeddingModel,
    ImageModel,
    PlainText,
)
from typing import Any, List
import asyncio

client = OpenAI(api_key=settings.OPENAI_API_KEY)


async def _alert_openai_out_of_credit(error_message: str) -> None:
    """OpenAI gives no API to check remaining balance (a long-standing, known
    gap in their own API, not something specific to us) — the only signal an
    app ever gets is this exact error, the moment a real call fails. This is
    the one place every AI call in the platform passes through, so it's the
    one place that can turn "a customer complains days later" into "the
    admin sees it on their dashboard, and gets an email, the moment it
    actually happens" (app/services/platform_alerts.py — not Sentry, which
    nobody is actively watching). get_db() is called directly rather than
    threaded through as a parameter: this static service has no request
    context to inject a db handle from, and get_db() is the same
    already-connected client every request-scoped dependency uses. Never
    allowed to break the real error path if the DB or email is unreachable."""
    try:
        from app.database import get_db
        from app.services import platform_alerts
        await platform_alerts.raise_alert(
            get_db(),
            kind="openai_insufficient_quota",
            title="OpenAI account is out of credit",
            detail=(
                "Every AI feature on the platform (Content Calendar, Social Listening, and anything "
                "else that calls OpenAI) is down until the balance is topped up or auto-reload is "
                f"enabled on the OpenAI account. Raw error: {error_message[:300]}"
            ),
        )
    except Exception as e:
        print(f"[AIService] failed to raise the out-of-credit platform alert: {e}")


class AIService:
    @staticmethod
    def construct_user_prompt(prompt: str):
        return {"role": "user", "content": prompt}

    @staticmethod
    def extract_ai_result(ai_response):
        if isinstance(ai_response, dict):
            return ai_response["choices"][0]["message"]["parsed"]
        return ai_response.choices[0].message.parsed

    @staticmethod
    def build_ai_model(
        messages: List[dict],
        model: str = "gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int = None,
    ):
        return ChatModel(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
        )

    @staticmethod
    async def chat_completion(request: ChatModel):
        try:
            loop = asyncio.get_running_loop()
            kwargs = dict(
                model=request.model,
                messages=[message.dict() for message in request.messages],
                temperature=request.temperature,
            )
            if request.max_tokens:
                kwargs["max_tokens"] = request.max_tokens
            completion = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(**kwargs),
            )
            return completion
        except Exception as e:
            error_message = str(e)

            # insufficient_quota is checked BEFORE the generic RateLimitError/429
            # branch below — OpenAI raises the exact same RateLimitError class and
            # 429 status for both a real rate limit AND a zero/negative account
            # balance, so the generic check used to catch every insufficient_quota
            # error first and this branch was unreachable dead code. That's why
            # this service could never actually report "out of credit" even
            # though it tried to — every failure of either kind came out as the
            # same generic "temporarily unavailable" message.
            if "insufficient_quota" in error_message:
                print("⚠️ OpenAI quota insufficient - credits exhausted")
                await _alert_openai_out_of_credit(error_message)
                return {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}

            if "RateLimitError" in str(type(e)) or "429" in error_message:
                print("⚠️ OpenAI rate limit exceeded")
                return {"error": "AI service is temporarily unavailable. Please try again later or contact support."}

            if "authentication" in error_message.lower() or "401" in error_message:
                print("⚠️ OpenAI authentication error - invalid API key")
                return {"error": "AI service configuration error. Please contact support."}

            if "timeout" in error_message.lower() or "timed out" in error_message.lower():
                print("⚠️ OpenAI request timed out")
                return {"error": "AI service request timed out. Please try again."}

            print(f"⚠️ Unexpected OpenAI error in chat_completion: {error_message}")
            raise e

    @staticmethod
    async def structured_chat_completion(
        request: ChatModel, response_model: Any = PlainText
    ):
        try:
            loop = asyncio.get_running_loop()

            completion = await loop.run_in_executor(
                None,
                lambda: client.beta.chat.completions.parse(
                    model=request.model,
                    messages=[message.dict() for message in request.messages],
                    response_format=response_model,
                    temperature=request.temperature,
                    max_tokens=2000,
                ),
            )

        except Exception as e:
            error_message = str(e)

            if "LengthFinishReasonError" in error_message:
                print("⚠️ OpenAI response truncated due to length limit")
                return {"error": "Response was truncated due to length limit."}

            # See chat_completion's matching comment: insufficient_quota must be
            # checked before the generic RateLimitError/429 branch, since OpenAI
            # raises the same exception class and status for both.
            if "insufficient_quota" in error_message:
                print("⚠️ OpenAI quota insufficient - credits exhausted")
                await _alert_openai_out_of_credit(error_message)
                return {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}

            if "RateLimitError" in str(type(e)) or "429" in error_message:
                print("⚠️ OpenAI rate limit exceeded")
                return {"error": "AI service is temporarily unavailable. Please try again later or contact support."}

            if "authentication" in error_message.lower() or "401" in error_message:
                print("⚠️ OpenAI authentication error - invalid API key")
                return {"error": "AI service configuration error. Please contact support."}

            if "timeout" in error_message.lower() or "timed out" in error_message.lower():
                print("⚠️ OpenAI request timed out")
                return {"error": "AI service request timed out. Please try again."}

            print(f"⚠️ Unexpected OpenAI error: {error_message}")
            raise e

        return completion

    @staticmethod
    async def create_embedding(request: EmbeddingModel):
        response = client.embeddings.create(
            model="text-embedding-3-small", input=request.input
        )
        return response
