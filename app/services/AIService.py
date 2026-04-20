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


class AIService:
    @staticmethod
    def construct_user_prompt(prompt: str):
        return {"role": "user", "content": prompt}

    @staticmethod
    def extract_ai_result(ai_response):
        try:
            # Check if response is an error dict
            if isinstance(ai_response, dict) and "error" in ai_response:
                raise ValueError(ai_response["error"])

            # Try object attribute access first
            if hasattr(ai_response, 'choices'):
                return ai_response.choices[0].message.parsed

            # Try dict access
            if isinstance(ai_response, dict):
                return ai_response["choices"][0]["message"]["parsed"]

            # Last resort: convert to dict and try
            return dict(ai_response)["choices"][0]["message"]["parsed"]
        except (AttributeError, KeyError, IndexError, TypeError) as e:
            print(f"❌ Error extracting AI result: {e}")
            print(f"📋 AI response type: {type(ai_response)}")
            print(f"📋 AI response: {ai_response}")
            raise

    @staticmethod
    def build_ai_model(
        messages: List[dict], model: str = "gpt-5.4-mini", temperature: float = 0.7
    ):
        return ChatModel(model=model, messages=messages, temperature=temperature)

    @staticmethod
    async def chat_completion(request: ChatModel):
        try:
            loop = asyncio.get_running_loop()
            completion = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model=request.model,
                    messages=[message.dict() for message in request.messages],
                    temperature=request.temperature,
                ),
            )
            print("Chat Completion Messages: ", [message.dict() for message in request.messages])
            print("Chat Completion Response: ", completion)
            return completion
        except Exception as e:
            error_message = str(e)

            if "RateLimitError" in str(type(e)) or "429" in error_message:
                print("⚠️ OpenAI rate limit exceeded - quota exhausted")
                return {"error": "AI service is temporarily unavailable. Please try again later or contact support."}

            if "insufficient_quota" in error_message:
                print("⚠️ OpenAI quota insufficient - credits exhausted")
                return {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}

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

            if "RateLimitError" in str(type(e)) or "429" in error_message:
                print("⚠️ OpenAI rate limit exceeded - quota exhausted")
                return {"error": "AI service is temporarily unavailable. Please try again later or contact support."}

            if "insufficient_quota" in error_message:
                print("⚠️ OpenAI quota insufficient - credits exhausted")
                return {"error": "AI service is temporarily unavailable due to quota limits. Please contact support."}

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
