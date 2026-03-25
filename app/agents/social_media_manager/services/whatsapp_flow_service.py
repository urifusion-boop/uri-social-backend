"""
WhatsApp conversation flow service.

States
------
linked              → phone linked, never messaged us yet
idle                → main menu shown
awaiting_content_prompt → waiting for the user to type their topic
showing_content     → content generated, follow-up options presented
showing_graphic     → graphic sent, download / post options presented
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.agents.social_media_manager.services.content_generation_service import (
    ContentGenerationService,
)
from app.agents.social_media_manager.services.image_content_service import (
    ImageContentService,
)
from app.agents.social_media_manager.services.whatsapp_session_service import (
    WhatsAppSessionService,
)
from app.core.config import settings

# ── Twilio client (lazy) ───────────────────────────────────────────────────


def _twilio_client():
    from twilio.rest import Client  # type: ignore

    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


# ── Message templates ─────────────────────────────────────────────────────

MAIN_MENU = (
    "What would you like to do today?\n\n"
    "1️⃣  Create content\n"
    "2️⃣  View content plan\n"
    "3️⃣  Generate graphic\n\n"
    "Reply with the number or keyword."
)

CONTENT_OPTIONS = (
    "Here's your content 👆\n\n"
    "What's next?\n"
    "1️⃣  Generate graphic for this\n"
    "2️⃣  Regenerate (try again)\n"
    "3️⃣  Main menu"
)

GRAPHIC_OPTIONS = (
    "Your graphic is ready 👆\n\n"
    "What's next?\n"
    "1️⃣  Download / share link\n"
    "2️⃣  Generate another\n"
    "3️⃣  Main menu"
)

NOT_LINKED = (
    "Hi 👋  I don't recognise this number.\n\n"
    "To get started, open your Uri Social dashboard and tap *Connect WhatsApp* "
    "in the onboarding section."
)

NO_BRAND = (
    "Your brand profile isn't set up yet.\n\n"
    "Please complete onboarding on the Uri Social dashboard first, "
    "then come back here."
)

GREETING_KEYWORDS = {"hi", "hello", "hey", "start", "menu", "help"}
CREATE_KEYWORDS = {"1", "create", "create content", "content"}
GRAPHIC_KEYWORDS = {"3", "graphic", "generate graphic", "image"}
PLAN_KEYWORDS = {"2", "plan", "view plan", "content plan", "view content plan"}
REGENERATE_KEYWORDS = {"2", "regenerate", "try again", "redo"}
MAIN_MENU_KEYWORDS = {"3", "menu", "main menu", "back"}
DOWNLOAD_KEYWORDS = {"1", "download", "share", "link"}


# ── Helpers ───────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    return text.strip().lower()


def _send(to: str, body: str, media_url: Optional[str] = None) -> None:
    """Send a WhatsApp message via Twilio."""
    client = _twilio_client()
    kwargs: Dict[str, Any] = {
        "from_": settings.TWILIO_WHATSAPP_FROM,
        "to": f"whatsapp:{to}" if not to.startswith("whatsapp:") else to,
        "body": body,
    }
    if media_url:
        kwargs["media_url"] = [media_url]
    client.messages.create(**kwargs)


async def _brand_context(user_id: str, db: AsyncIOMotorDatabase) -> Optional[Dict[str, Any]]:
    profile = await WhatsAppSessionService.get_brand_profile(user_id, db)
    if not profile:
        return None
    return {
        "brand_name": profile.get("brand_name", ""),
        "industry": profile.get("industry", ""),
        "brand_voice": profile.get("derived_voice", ""),
        "voice_sample": profile.get("voice_sample", ""),
        "brand_colors": profile.get("brand_colors", []),
        "logo_url": profile.get("logo_url"),
        "key_products_services": profile.get("key_products_services", []),
        "website": profile.get("website", ""),
        "tagline": profile.get("tagline", ""),
        "content_pillars": profile.get("content_pillars", []),
    }


# ── Main dispatcher ───────────────────────────────────────────────────────


class WhatsAppFlowService:
    @staticmethod
    async def handle(
        raw_from: str,
        body: str,
        db: AsyncIOMotorDatabase,
    ) -> None:
        """
        Entry point called by the webhook for every incoming message.
        Looks up the user, loads session, dispatches to the right handler.
        """
        phone = WhatsAppSessionService._normalize_phone(raw_from)
        text = _norm(body)

        # ── Identify user ──────────────────────────────────────────────────
        user = await WhatsAppSessionService.get_user_by_phone(phone, db)
        if not user:
            _send(phone, NOT_LINKED)
            return

        user_id: str = user["userId"]
        first_name: str = user.get("first_name") or "there"

        # ── Load / init session ────────────────────────────────────────────
        session = await WhatsAppSessionService.get_session(phone, db) or {}
        state: str = session.get("state", "linked")
        context: Dict[str, Any] = session.get("context", {})

        # ── Always-available shortcuts ─────────────────────────────────────
        if text in GREETING_KEYWORDS or state in ("linked",):
            await WhatsAppFlowService._send_welcome(phone, first_name, user_id, db)
            return

        if text in MAIN_MENU_KEYWORDS and state not in ("awaiting_content_prompt",):
            _send(phone, MAIN_MENU)
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)
            return

        # ── Route by state ─────────────────────────────────────────────────
        if state == "showing_graphic":
            await WhatsAppFlowService._handle_graphic_options(
                phone, text, user_id, context, db
            )

        elif state == "idle":
            await WhatsAppFlowService._handle_menu(
                phone, text, user_id, first_name, context, db
            )

        elif state == "awaiting_content_prompt":
            await WhatsAppFlowService._generate_content(
                phone, body, user_id, context, db
            )

        elif state == "showing_content":
            await WhatsAppFlowService._handle_content_options(
                phone, text, user_id, context, db
            )

        else:
            # Fallback — show menu
            _send(phone, MAIN_MENU)
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)

    # ── Handlers ──────────────────────────────────────────────────────────

    @staticmethod
    async def _send_welcome(
        phone: str, first_name: str, user_id: str, db: AsyncIOMotorDatabase
    ) -> None:
        brand = await WhatsAppSessionService.get_brand_profile(user_id, db)
        brand_name = brand.get("brand_name", "") if brand else ""
        greeting = (
            f"Hi {first_name} 👋\n\n"
            f"{'Your *' + brand_name + '* brand is all set. ' if brand_name else ''}"
            "Your Uri Social account is ready.\n\n"
            + MAIN_MENU
        )
        _send(phone, greeting)
        await WhatsAppSessionService.set_state(phone, "idle", {}, db)

    @staticmethod
    async def _handle_menu(
        phone: str,
        text: str,
        user_id: str,
        first_name: str,
        context: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        if text in CREATE_KEYWORDS:
            _send(phone, "What do you want to post about? Type your topic or idea.")
            await WhatsAppSessionService.set_state(
                phone, "awaiting_content_prompt", {}, db
            )

        elif text in PLAN_KEYWORDS:
            await WhatsAppFlowService._send_content_plan(phone, user_id, db)

        elif text in GRAPHIC_KEYWORDS:
            # No prior content — ask for a topic first then generate graphic
            _send(phone, "What do you want to post about? Type your topic or idea.")
            await WhatsAppSessionService.set_state(
                phone, "awaiting_content_prompt", {"skip_to_graphic": True}, db
            )

        else:
            _send(phone, "I didn't quite get that.\n\n" + MAIN_MENU)

    @staticmethod
    async def _generate_content(
        phone: str,
        prompt: str,
        user_id: str,
        context: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        _send(phone, "Generating your content... ✍️  (this takes a few seconds)")

        brand = await _brand_context(user_id, db)
        if brand is None:
            _send(phone, NO_BRAND)
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)
            return

        result = await ContentGenerationService.generate_multi_platform_content(
            user_id=user_id,
            seed_content=prompt,
            platforms=["instagram"],
            seed_type="text",
            brand_context=brand,
            db=db,
        )

        if not result.get("status"):
            _send(phone, "Sorry, I couldn't generate content right now. Please try again.")
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)
            return

        drafts: List[Dict[str, Any]] = result["responseData"]["drafts"]
        if not drafts:
            _send(phone, "No content was generated. Please try a different topic.")
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)
            return

        draft = drafts[0]
        content_text: str = draft["content"]
        draft_id: str = draft["id"]

        hashtags = draft.get("hashtags", [])
        hashtag_line = "  ".join(f"#{h}" for h in hashtags[:8]) if hashtags else ""
        full_message = content_text
        if hashtag_line:
            full_message += f"\n\n{hashtag_line}"

        _send(phone, full_message)
        _send(phone, CONTENT_OPTIONS)

        new_context = {
            "last_prompt": prompt,
            "last_draft_id": draft_id,
            "last_content": content_text,
            "skip_to_graphic": context.get("skip_to_graphic", False),
        }

        # If user originally wanted a graphic, jump straight to it
        if context.get("skip_to_graphic"):
            await WhatsAppFlowService._generate_graphic(phone, user_id, new_context, db)
        else:
            await WhatsAppSessionService.set_state(phone, "showing_content", new_context, db)

    @staticmethod
    async def _handle_content_options(
        phone: str,
        text: str,
        user_id: str,
        context: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        if text in {"1", "graphic", "generate graphic"}:
            await WhatsAppFlowService._generate_graphic(phone, user_id, context, db)

        elif text in REGENERATE_KEYWORDS:
            last_prompt = context.get("last_prompt", "")
            if last_prompt:
                await WhatsAppFlowService._generate_content(
                    phone, last_prompt, user_id, {}, db
                )
            else:
                _send(phone, "I don't have your previous topic. Please type your topic again.")
                await WhatsAppSessionService.set_state(
                    phone, "awaiting_content_prompt", {}, db
                )

        elif text in MAIN_MENU_KEYWORDS:
            _send(phone, MAIN_MENU)
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)

        else:
            _send(phone, "Please reply:\n1 – Generate graphic\n2 – Regenerate\n3 – Main menu")

    @staticmethod
    async def _handle_graphic_options(
        phone: str,
        text: str,
        user_id: str,
        context: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        """Handle user replies after a graphic has been delivered."""

        if text in DOWNLOAD_KEYWORDS:
            url = context.get("last_graphic_url")
            if url:
                _send(phone, f"Here is your graphic link 🔗\n{url}\n\nYou can save or share it directly.")
            else:
                _send(phone, "Sorry, the graphic link is no longer available. Try generating a new one.")
            _send(phone, GRAPHIC_OPTIONS)

        elif text in {"2", "another", "generate another", "new graphic"}:
            # Re-generate graphic using same content context
            await WhatsAppFlowService._generate_graphic(phone, user_id, context, db)

        elif text in MAIN_MENU_KEYWORDS:
            _send(phone, MAIN_MENU)
            await WhatsAppSessionService.set_state(phone, "idle", {}, db)

        elif text in CREATE_KEYWORDS:
            # Allow jumping straight to a new content prompt from graphic state
            _send(phone, "What do you want to post about? Type your topic or idea.")
            await WhatsAppSessionService.set_state(
                phone, "awaiting_content_prompt", {}, db
            )

        else:
            _send(
                phone,
                "Please reply:\n"
                "1 – Download / share link\n"
                "2 – Generate another\n"
                "3 – Main menu",
            )

    @staticmethod
    async def _generate_graphic(
        phone: str,
        user_id: str,
        context: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        _send(phone, "Creating your graphic... 🎨  (this may take 10–20 seconds)")

        brand = await _brand_context(user_id, db)
        seed = context.get("last_content") or context.get("last_prompt", "content graphic")

        try:
            image_result = await ImageContentService._generate_platform_image(
                platform="instagram",
                content=seed,
                seed_content=seed,
                brand_context=brand,
            )
        except Exception as exc:
            print(f"WhatsApp graphic error: {exc}")
            _send(phone, "Sorry, I couldn't generate the graphic right now. Please try again.")
            await WhatsAppSessionService.set_state(phone, "showing_content", context, db)
            return

        if not image_result.get("status"):
            _send(phone, "Graphic generation failed. Please try again.")
            await WhatsAppSessionService.set_state(phone, "showing_content", context, db)
            return

        raw_url: str = image_result["responseData"]["image_url"]

        # Upload base64 to imgBB so we have a public URL for Twilio
        public_url: Optional[str] = None
        if raw_url.startswith("data:") and settings.IMGBB_API_KEY:
            try:
                import re as _re
                import httpx

                match = _re.match(r"data:[^;]+;base64,(.+)", raw_url, _re.DOTALL)
                if match:
                    # Strip any whitespace/newlines that break multipart uploads
                    b64_clean = match.group(1).strip().replace("\n", "").replace("\r", "")
                    async with httpx.AsyncClient(timeout=60) as c:
                        r = await c.post(
                            "https://api.imgbb.com/1/upload",
                            data={"key": settings.IMGBB_API_KEY, "image": b64_clean},
                        )
                        rj = r.json()
                    if rj.get("success"):
                        public_url = rj["data"]["url"]
                    else:
                        print(f"imgBB upload failed: {rj}")
                else:
                    print(f"imgBB: could not parse data URL (prefix: {raw_url[:50]})")
            except Exception as e:
                print(f"imgBB upload error in WhatsApp flow: {e}")
        elif raw_url.startswith("http"):
            public_url = raw_url

        if public_url:
            _send(phone, "Here's your graphic:", media_url=public_url)
        else:
            _send(phone, "Graphic ready! (image preview unavailable — check your dashboard)")

        _send(phone, GRAPHIC_OPTIONS)
        await WhatsAppSessionService.set_state(
            phone,
            "showing_graphic",
            {**context, "last_graphic_url": public_url},
            db,
        )

    @staticmethod
    async def _send_content_plan(
        phone: str, user_id: str, db: AsyncIOMotorDatabase
    ) -> None:
        """Generate and send 3 quick content ideas for today."""
        brand = await _brand_context(user_id, db)
        industry = (brand or {}).get("industry", "your industry")
        brand_name = (brand or {}).get("brand_name", "")

        prompt = (
            f"Give me 3 short, specific social media content ideas for today "
            f"for a {'brand called ' + brand_name + ' in the ' if brand_name else ''}"
            f"{industry} space. Format as a numbered list. Keep each idea to one sentence."
        )

        from app.services.AIService import AIService
        from app.domain.models.chat_model import ChatModel, ChatMessage

        messages = [
            ChatMessage(role="system", content="You are a social media strategist."),
            ChatMessage(role="user", content=prompt),
        ]
        request = ChatModel(model="gpt-4o-mini", messages=messages, temperature=0.8)
        result = await AIService.chat_completion(request)

        if isinstance(result, dict) and result.get("error"):
            _send(phone, "Could not load content plan right now. Try again shortly.")
        else:
            ideas = result.choices[0].message.content.strip()
            _send(phone, f"*Your content ideas for today:*\n\n{ideas}\n\n{MAIN_MENU}")

        await WhatsAppSessionService.set_state(phone, "idle", {}, db)

    # ── Daily push ────────────────────────────────────────────────────────

    @staticmethod
    async def send_daily_push(db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        """
        Send a daily content idea to every user with a linked WhatsApp number.
        Designed to be called from a scheduled job / cron endpoint.
        """
        users = await WhatsAppSessionService.get_all_linked_users(db)
        sent = 0
        failed = 0

        for user in users:
            phone: str = user.get("whatsapp_phone", "")
            first_name: str = user.get("first_name") or "there"
            user_id: str = user.get("userId", "")
            if not phone:
                continue
            try:
                brand = await WhatsAppSessionService.get_brand_profile(user_id, db)
                industry = (brand or {}).get("industry", "your industry")
                brand_name = (brand or {}).get("brand_name", "")

                prompt = (
                    f"Give me 1 punchy social media content idea for today for "
                    f"{'a brand called ' + brand_name + ' in the ' if brand_name else ''}"
                    f"{industry} space. One sentence."
                )

                from app.services.AIService import AIService
                from app.domain.models.chat_model import ChatModel, ChatMessage

                messages = [
                    ChatMessage(role="system", content="You are a social media strategist."),
                    ChatMessage(role="user", content=prompt),
                ]
                req = ChatModel(model="gpt-4o-mini", messages=messages, temperature=0.9)
                ai = await AIService.chat_completion(req)

                idea = (
                    ai.choices[0].message.content.strip()
                    if not isinstance(ai, dict)
                    else "Keep showing up — consistency is the strategy."
                )

                body = (
                    f"Hi {first_name} 👋\n\n"
                    f"*Your content idea for today:*\n{idea}\n\n"
                    "Reply *create* to turn it into a full post, or *graphic* to generate an image."
                )
                _send(phone, body)
                sent += 1
            except Exception as e:
                print(f"Daily push failed for {phone}: {e}")
                failed += 1

        return {"sent": sent, "failed": failed, "total": len(users)}
