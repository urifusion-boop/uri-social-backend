"""
WhatsApp conversation flow service — Uri Social assistant.

Design principle: Remove thinking.
User opens WhatsApp → sees content → taps once → posts.

States
------
linked                → first-time user — immediately generate content
idle                  → returning user waiting for a command
showing_content       → headline + subheadline + caption displayed
showing_ideas         → 3 quick ideas shown, awaiting choice 1/2/3
awaiting_topic        → "create post" sent without a topic
awaiting_edit_choice  → edit options shown (1=headline 2=subheadline 3=tone)
awaiting_edit_value   → waiting for the new value to apply
showing_graphic       → graphic ready, post actions displayed
awaiting_re_engagement → re-engagement ping sent, waiting for yes/later
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.agents.social_media_manager.services.image_content_service import (
    ImageContentService,
)
from app.agents.social_media_manager.services.whatsapp_session_service import (
    WhatsAppSessionService,
)
from app.core.config import settings

# ── Twilio client (lazy) ──────────────────────────────────────────────────────


def _twilio_client():
    from twilio.rest import Client  # type: ignore

    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


# ── Send helper ───────────────────────────────────────────────────────────────


def _send(to: str, body: str, media_url: Optional[str] = None) -> None:
    client = _twilio_client()
    kwargs: Dict[str, Any] = {
        "from_": settings.TWILIO_WHATSAPP_FROM,
        "to": f"whatsapp:{to}" if not to.startswith("whatsapp:") else to,
        "body": body,
    }
    if media_url:
        kwargs["media_url"] = [media_url]
    client.messages.create(**kwargs)


# ── Static messages ───────────────────────────────────────────────────────────

NOT_LINKED = (
    "Hi 👋  I don't recognise this number.\n\n"
    "Open your Uri Social dashboard and tap *Connect WhatsApp* to get started."
)

NO_BRAND = (
    "Your brand profile isn't set up yet.\n\n"
    "Complete onboarding on the Uri Social dashboard first, then come back here."
)

CONTENT_ACTIONS = (
    "1️⃣  Generate graphic\n"
    "2️⃣  View full caption\n"
    "3️⃣  Change idea"
)

GRAPHIC_ACTIONS = (
    "1️⃣  Download\n"
    "2️⃣  Edit text\n"
    "3️⃣  Regenerate design"
)

EDIT_ACTIONS = (
    "What would you like to change?\n\n"
    "1️⃣  Headline\n"
    "2️⃣  Subheadline\n"
    "3️⃣  Tone"
)

RE_ENGAGEMENT = (
    "We created fresh content ideas for you.\n"
    "Want to see them?\n\n"
    "1️⃣  Yes\n"
    "2️⃣  Later"
)

# ── Command patterns ──────────────────────────────────────────────────────────

_CREATE_ABOUT = re.compile(
    r"(?:create\s+(?:a\s+)?post\s+about|post\s+about|write\s+(?:a\s+)?post\s+about)\s+(.+)",
    re.IGNORECASE,
)

IDEAS_KEYWORDS = {"give me ideas", "ideas", "idea", "suggestions"}
GRAPHIC_KEYWORDS = {"generate graphic", "graphic", "image", "design"}
CREATE_KEYWORDS = {"create", "create post", "create content", "new post"}
GREETING_KEYWORDS = {"hi", "hello", "hey", "start", "help"}

_EMOJI_DIGITS = {"1️⃣": "1", "2️⃣": "2", "3️⃣": "3", "4️⃣": "4"}


def _opt(text: str) -> Optional[str]:
    """
    Normalise a user reply to a plain digit string ("1"–"4") or None.
    Handles: "1", "1.", "1️⃣", "1 generate graphic", "  2  ", etc.
    """
    t = text.strip()
    if t in _EMOJI_DIGITS:
        return _EMOJI_DIGITS[t]
    # strip leading emoji digit
    for emoji, digit in _EMOJI_DIGITS.items():
        if t.startswith(emoji):
            return digit
    # plain digit optionally followed by non-digit chars
    if t and t[0] in "1234":
        rest = t[1:].lstrip(" .")
        if not rest or not rest[0].isdigit():
            return t[0]
    return None


# ── Brand context ─────────────────────────────────────────────────────────────


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


# ── AI content generation ─────────────────────────────────────────────────────


async def _generate_content_structured(
    topic: str,
    brand: Dict[str, Any],
    tone: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """
    Returns {"headline": str, "subheadline": str, "caption": str} or None on failure.
    """
    from app.domain.models.chat_model import ChatMessage, ChatModel
    from app.services.AIService import AIService

    brand_name = brand.get("brand_name", "")
    industry = brand.get("industry", "")
    voice = brand.get("brand_voice", "professional and engaging")
    tone_line = f"Tone: {tone}." if tone else f"Brand voice: {voice}."

    prompt = (
        f"Create a social media post for {brand_name or 'a brand'}"
        f"{' in the ' + industry + ' space' if industry else ''}.\n"
        f"Topic: {topic}\n"
        f"{tone_line}\n\n"
        "Return ONLY this exact format — no extra text:\n"
        "Headline: [punchy, quotable headline]\n"
        "Subheadline: [one supporting line]\n"
        "Caption: [2–3 sentence caption that opens with a hook]"
    )

    messages = [
        ChatMessage(
            role="system",
            content="You are a social media content creator. Return content in the exact format requested.",
        ),
        ChatMessage(role="user", content=prompt),
    ]
    req = ChatModel(model="gpt-5.4-mini", messages=messages, temperature=0.8)
    result = await AIService.chat_completion(req)

    if isinstance(result, dict) and result.get("error"):
        return None

    text = result.choices[0].message.content.strip()
    parsed: Dict[str, str] = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.lower().startswith("headline:"):
            parsed["headline"] = line[9:].strip().strip('"')
        elif line.lower().startswith("subheadline:"):
            parsed["subheadline"] = line[12:].strip().strip('"')
        elif line.lower().startswith("caption:"):
            parsed["caption"] = line[8:].strip()

    return parsed if parsed.get("headline") else None


async def _generate_three_ideas(topic: str, brand: Dict[str, Any]) -> Optional[List[str]]:
    """Returns 3 headline-only ideas for the user to pick from."""
    from app.domain.models.chat_model import ChatMessage, ChatModel
    from app.services.AIService import AIService

    brand_name = brand.get("brand_name", "")
    industry = brand.get("industry", "")

    prompt = (
        f"Give me 3 punchy, quotable post headlines for {brand_name or 'a brand'}"
        f"{' in the ' + industry + ' space' if industry else ''}.\n"
        f"Topic: {topic or 'anything relevant to the brand'}\n\n"
        "Return ONLY:\n"
        "1. [headline]\n"
        "2. [headline]\n"
        "3. [headline]"
    )

    messages = [
        ChatMessage(role="system", content="You are a social media strategist."),
        ChatMessage(role="user", content=prompt),
    ]
    req = ChatModel(model="gpt-5.4-mini", messages=messages, temperature=0.9)
    result = await AIService.chat_completion(req)

    if isinstance(result, dict) and result.get("error"):
        return None

    text = result.choices[0].message.content.strip()
    ideas = []
    for line in text.split("\n"):
        m = re.match(r"^\d+[\.\)]\s*(.+)", line.strip())
        if m:
            ideas.append(m.group(1).strip().strip('"'))
    return ideas[:3] if ideas else None


# ── Safe DB write (swallows MongoDB network timeouts) ─────────────────────────


async def _safe_set_state(
    phone: str,
    state: str,
    ctx: Optional[Dict[str, Any]],
    db: AsyncIOMotorDatabase,
) -> None:
    try:
        await WhatsAppSessionService.set_state(phone, state, ctx, db)
    except Exception as e:
        print(f"[WhatsApp] set_state failed (non-fatal): {e}")


# ── Display helpers ───────────────────────────────────────────────────────────


def _format_content(ctx: Dict[str, Any]) -> str:
    headline = ctx.get("headline", "")
    subheadline = ctx.get("subheadline", "")
    caption = ctx.get("caption", "")
    preview = caption[:120] + "…" if len(caption) > 120 else caption
    return (
        f'Headline: "{headline}"\n'
        f'Subheadline: "{subheadline}"\n\n'
        f"Caption preview: {preview}\n\n"
        + CONTENT_ACTIONS
    )


# ── Main dispatcher ───────────────────────────────────────────────────────────


class WhatsAppFlowService:

    @staticmethod
    async def handle(raw_from: str, body: str, db: AsyncIOMotorDatabase) -> None:
        phone = WhatsAppSessionService._normalize_phone(raw_from)
        text = body.strip().lower()

        user = await WhatsAppSessionService.get_user_by_phone(phone, db)
        if not user:
            _send(phone, NOT_LINKED)
            return

        user_id: str = user["userId"]
        first_name: str = user.get("first_name") or "there"

        session = await WhatsAppSessionService.get_session(phone, db) or {}
        state: str = session.get("state", "linked")
        ctx: Dict[str, Any] = session.get("context", {})

        # ── First-time user ────────────────────────────────────────────────
        if state == "linked":
            await WhatsAppFlowService._first_time_entry(phone, first_name, user_id, db)
            return

        # ── State-specific routing ─────────────────────────────────────────
        if state == "awaiting_topic":
            await WhatsAppFlowService._create_and_show_content(phone, body.strip(), user_id, ctx, db)
            return

        if state == "awaiting_edit_choice":
            await WhatsAppFlowService._handle_edit_choice(phone, text, user_id, ctx, db)
            return

        if state == "awaiting_edit_value":
            await WhatsAppFlowService._apply_edit(phone, body.strip(), user_id, ctx, db)
            return

        if state == "showing_content":
            await WhatsAppFlowService._handle_content_actions(phone, text, user_id, ctx, db)
            return

        if state == "showing_ideas":
            await WhatsAppFlowService._handle_ideas_pick(phone, text, user_id, ctx, db)
            return

        if state == "showing_graphic":
            await WhatsAppFlowService._handle_graphic_actions(phone, text, user_id, ctx, db)
            return

        if state == "awaiting_re_engagement":
            await WhatsAppFlowService._handle_re_engagement(phone, text, user_id, ctx, db)
            return

        # ── Idle / fallback — parse commands ───────────────────────────────
        await WhatsAppFlowService._handle_idle(phone, text, body.strip(), user_id, first_name, ctx, db)

    # ── First-time entry ───────────────────────────────────────────────────────

    @staticmethod
    async def _first_time_entry(
        phone: str, first_name: str, user_id: str, db: AsyncIOMotorDatabase
    ) -> None:
        brand = await _brand_context(user_id, db)
        if not brand:
            _send(phone, f"Hi {first_name} 👋\n\nYour Uri Social account is ready.\n\n" + NO_BRAND)
            await _safe_set_state(phone, "idle", {}, db)
            return

        _send(
            phone,
            f"Hi {first_name} 👋\n\nYour Uri Social account is ready.\n"
            "I will help you create content daily based on your brand.\n\n"
            "Here is a content idea for you today:",
        )

        industry = brand.get("industry", "your niche")
        topic = f"a powerful truth about {industry}"
        await WhatsAppFlowService._create_and_show_content(phone, topic, user_id, {}, db)

    # ── Idle command parser ────────────────────────────────────────────────────

    @staticmethod
    async def _handle_idle(
        phone: str,
        text: str,
        raw_body: str,
        user_id: str,
        first_name: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        # "create post about [topic]"
        m = _CREATE_ABOUT.match(raw_body)
        if m:
            await WhatsAppFlowService._create_and_show_content(
                phone, m.group(1).strip(), user_id, {}, db
            )
            return

        # "give me ideas"
        if text in IDEAS_KEYWORDS:
            await WhatsAppFlowService._send_ideas(phone, user_id, "", db)
            return

        # "generate graphic" with last content in context
        if text in GRAPHIC_KEYWORDS and ctx.get("headline"):
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
            return

        # "create post" without topic
        if text in CREATE_KEYWORDS:
            _send(phone, "What do you want to post about?")
            await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        # Greeting — re-show last content or prompt
        if text in GREETING_KEYWORDS:
            if ctx.get("headline"):
                _send(phone, f"Welcome back {first_name} 👋\n\nHere's your last content:\n\n" + _format_content(ctx))
                await _safe_set_state(phone, "showing_content", ctx, db)
            else:
                _send(phone, f"Hi {first_name} 👋  What do you want to post about today?")
            return

        # Anything else — treat as topic
        await WhatsAppFlowService._create_and_show_content(phone, raw_body, user_id, {}, db)

    # ── Content generation & display ──────────────────────────────────────────

    @staticmethod
    async def _create_and_show_content(
        phone: str,
        topic: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        brand = await _brand_context(user_id, db)
        if not brand:
            _send(phone, NO_BRAND)
            await _safe_set_state(phone, "idle", {}, db)
            return

        _send(phone, "Creating your content... ✍️")

        tone = ctx.get("tone")
        content = await _generate_content_structured(topic, brand, tone=tone)
        if not content:
            _send(phone, "Could not generate content right now. Please try again.")
            await _safe_set_state(phone, "idle", {}, db)
            return

        new_ctx = {
            "topic": topic,
            "headline": content["headline"],
            "subheadline": content["subheadline"],
            "caption": content["caption"],
            "tone": tone,
        }

        _send(phone, _format_content(new_ctx))
        await _safe_set_state(phone, "showing_content", new_ctx, db)

    # ── Content action handler (state: showing_content) ───────────────────────

    @staticmethod
    async def _handle_content_actions(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        opt = _opt(text)

        if opt == "1" or text in {"generate graphic", "graphic"}:
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)

        elif opt == "2" or text in {"view full caption", "caption", "full caption"}:
            caption = ctx.get("caption", "No caption saved.")
            _send(phone, f"Full caption:\n\n{caption}\n\n" + CONTENT_ACTIONS)

        elif opt == "3" or text in {"change idea", "change", "another"}:
            topic = ctx.get("topic", "")
            await WhatsAppFlowService._create_and_show_content(phone, topic, user_id, ctx, db)

        elif opt == "4" or text == "edit":
            _send(phone, EDIT_ACTIONS)
            await _safe_set_state(phone, "awaiting_edit_choice", ctx, db)

        else:
            _send(phone, _format_content(ctx))

    # ── Ideas flow ────────────────────────────────────────────────────────────

    @staticmethod
    async def _send_ideas(
        phone: str, user_id: str, topic: str, db: AsyncIOMotorDatabase
    ) -> None:
        brand = await _brand_context(user_id, db)
        if not brand:
            _send(phone, NO_BRAND)
            await _safe_set_state(phone, "idle", {}, db)
            return

        ideas = await _generate_three_ideas(topic, brand)
        if not ideas:
            _send(phone, "Could not generate ideas right now. Try again shortly.")
            return

        lines = "\n".join(f"{i + 1}️⃣  {idea}" for i, idea in enumerate(ideas))
        _send(phone, f"Here are 3 ideas for you:\n\n{lines}\n\nReply 1, 2, or 3 to pick one.")
        await _safe_set_state(
            phone, "showing_ideas", {"ideas": ideas, "topic": topic}, db
        )

    @staticmethod
    async def _handle_ideas_pick(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        ideas: List[str] = ctx.get("ideas", [])
        pick_map = {"1": 0, "2": 1, "3": 2}
        opt = _opt(text)
        if opt in pick_map and pick_map[opt] < len(ideas):
            chosen = ideas[pick_map[opt]]
            await WhatsAppFlowService._create_and_show_content(phone, chosen, user_id, {}, db)
        else:
            lines = "\n".join(f"{i + 1}️⃣  {idea}" for i, idea in enumerate(ideas))
            _send(phone, f"Reply 1, 2, or 3:\n\n{lines}")

    # ── Edit flow ─────────────────────────────────────────────────────────────

    @staticmethod
    async def _handle_edit_choice(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        field_map = {"1": "headline", "2": "subheadline", "3": "tone"}
        prompts = {
            "headline": "Type your new headline:",
            "subheadline": "Type your new subheadline:",
            "tone": "What tone would you like? (e.g. motivational, professional, funny, bold)",
        }

        field = field_map.get(_opt(text) or text)
        if not field:
            _send(phone, EDIT_ACTIONS)
            return

        _send(phone, prompts[field])
        await _safe_set_state(
            phone, "awaiting_edit_value", {**ctx, "edit_field": field}, db
        )

    @staticmethod
    async def _apply_edit(
        phone: str,
        value: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        field = ctx.get("edit_field", "headline")

        if field == "tone":
            # Re-generate with new tone
            new_ctx = {**ctx, "tone": value}
            await WhatsAppFlowService._create_and_show_content(
                phone, ctx.get("topic", value), user_id, new_ctx, db
            )
        else:
            # Direct field replacement
            new_ctx = {**ctx, field: value}
            _send(phone, _format_content(new_ctx))
            await _safe_set_state(phone, "showing_content", new_ctx, db)

    # ── Graphic generation ────────────────────────────────────────────────────

    @staticmethod
    async def _generate_graphic(
        phone: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        _send(phone, "Creating your design... 🎨")

        brand = await _brand_context(user_id, db)
        headline = ctx.get("headline", "")
        subheadline = ctx.get("subheadline", "")
        caption = ctx.get("caption", "")
        seed = f"{headline} — {subheadline} — {caption}".strip(" —") or ctx.get("topic", "content graphic")

        try:
            image_result = await ImageContentService._generate_platform_image(
                platform="instagram",
                content=seed,
                seed_content=seed,
                brand_context=brand,
            )
        except Exception as exc:
            print(f"[WhatsApp] graphic generation error: {exc}")
            _send(phone, "Could not generate the graphic right now. Please try again.")
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        if not image_result.get("status"):
            _send(phone, "Graphic generation failed. Please try again.")
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        raw_url: str = image_result["responseData"]["image_url"]
        public_url: Optional[str] = None

        if raw_url.startswith("data:") and settings.IMGBB_API_KEY:
            try:
                import base64
                import io
                import httpx
                import re as _re
                from PIL import Image as PILImage

                match = _re.match(r"data:[^;]+;base64,(.+)", raw_url, _re.DOTALL)
                if match:
                    b64_clean = match.group(1).strip().replace("\n", "").replace("\r", "")
                    # Convert to JPEG — Twilio WhatsApp requires JPEG or PNG
                    raw_bytes = base64.b64decode(b64_clean)
                    img = PILImage.open(io.BytesIO(raw_bytes)).convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=92)
                    b64_jpeg = base64.b64encode(buf.getvalue()).decode("utf-8")

                    print(f"[WhatsApp] uploading JPEG graphic to imgBB ({len(b64_jpeg)} chars)...")
                    async with httpx.AsyncClient(timeout=60) as c:
                        r = await c.post(
                            "https://api.imgbb.com/1/upload",
                            data={"key": settings.IMGBB_API_KEY, "image": b64_jpeg},
                        )
                        rj = r.json()
                    if rj.get("success"):
                        public_url = rj["data"]["url"]
                        print(f"[WhatsApp] imgBB upload success: {public_url}")
                    else:
                        print(f"[WhatsApp] imgBB upload failed: {rj}")
                else:
                    print(f"[WhatsApp] could not parse data URL")
            except Exception as e:
                print(f"[WhatsApp] imgBB upload error: {e}")
        elif raw_url.startswith("http"):
            public_url = raw_url
            print(f"[WhatsApp] using direct image URL: {public_url}")

        if public_url:
            try:
                _send(phone, "Your design is ready 👆", media_url=public_url)
                print(f"[WhatsApp] image message sent: {public_url}")
            except Exception as e:
                print(f"[WhatsApp] Twilio media send failed: {e} — falling back to link")
                _send(phone, f"Your design is ready 👆\n\n🔗 {public_url}")
            _send(phone, GRAPHIC_ACTIONS)
        else:
            _send(phone, "Your design is ready 👆\n\nImage preview unavailable. Check your Uri Social dashboard to download it.")
            _send(phone, GRAPHIC_ACTIONS)

        await _safe_set_state(phone, "showing_graphic", {**ctx, "last_graphic_url": public_url}, db)

    # ── Graphic action handler (state: showing_graphic) ───────────────────────

    @staticmethod
    async def _handle_graphic_actions(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        opt = _opt(text)

        if opt == "1" or text == "download":
            url = ctx.get("last_graphic_url")
            if url:
                _send(phone, f"🔗 Your graphic:\n{url}\n\nBest time to post: 7PM today.")
            else:
                _send(phone, "Graphic link unavailable. Generate a new one below.")
                _send(phone, GRAPHIC_ACTIONS)
                return
            await _safe_set_state(phone, "idle", ctx, db)

        elif opt == "2" or text in {"edit text", "edit"}:
            _send(phone, EDIT_ACTIONS)
            await _safe_set_state(phone, "awaiting_edit_choice", ctx, db)

        elif opt == "3" or text in {"regenerate", "regenerate design", "new design"}:
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)

        else:
            _send(phone, GRAPHIC_ACTIONS)

    # ── Re-engagement handler ─────────────────────────────────────────────────

    @staticmethod
    async def _handle_re_engagement(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        if _opt(text) == "1" or text in {"yes", "yeah", "sure", "ok"}:
            brand = await _brand_context(user_id, db)
            industry = (brand or {}).get("industry", "your niche")
            await WhatsAppFlowService._create_and_show_content(
                phone, f"a powerful truth about {industry}", user_id, {}, db
            )
        else:
            _send(phone, "No problem. Reply *create* anytime you're ready.")
            await _safe_set_state(phone, "idle", {}, db)

    # ── Daily push ────────────────────────────────────────────────────────────

    @staticmethod
    async def send_daily_push(db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        """
        Morning push to all WhatsApp-linked users.
        Re-engagement ping for users inactive > 2 days.
        Called from the /whatsapp/daily-push cron endpoint.
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
                session = await WhatsAppSessionService.get_session(phone, db) or {}
                last_updated = session.get("updated_at")
                state = session.get("state", "idle")

                # Re-engagement if inactive for > 2 days
                if last_updated and state == "idle":
                    delta = datetime.utcnow() - last_updated
                    if delta.days >= 2:
                        _send(phone, RE_ENGAGEMENT)
                        await _safe_set_state(
                            phone, "awaiting_re_engagement", session.get("context", {}), db
                        )
                        sent += 1
                        continue

                brand = await _brand_context(user_id, db)
                if not brand:
                    continue

                industry = brand.get("industry", "your niche")
                topic = f"a powerful truth about {industry}"
                content = await _generate_content_structured(topic, brand)
                if not content:
                    failed += 1
                    continue

                new_ctx = {
                    "topic": topic,
                    "headline": content["headline"],
                    "subheadline": content["subheadline"],
                    "caption": content["caption"],
                }

                _send(
                    phone,
                    f"Good morning {first_name} 👋\n\nYour content for today is ready.",
                )
                _send(phone, _format_content(new_ctx))
                await _safe_set_state(phone, "showing_content", new_ctx, db)
                sent += 1

            except Exception as e:
                print(f"Daily push failed for {phone}: {e}")
                failed += 1

        return {"sent": sent, "failed": failed, "total": len(users)}
