"""
WhatsApp conversation flow service — Uri Social assistant.

Design principle: Remove thinking.
User opens WhatsApp → sees content → taps once → posts.

States
------
linked                   → first-time user — immediately generate content
idle                     → returning user waiting for a command
showing_content          → headline + subheadline + caption displayed
showing_ideas            → 3 quick ideas shown, awaiting choice 1/2/3
awaiting_topic           → "create post" sent without a topic
awaiting_edit_choice     → edit options shown (1=headline 2=subheadline 3=tone)
awaiting_edit_value      → waiting for the new value to apply
showing_graphic          → graphic ready, post actions displayed
awaiting_re_engagement   → re-engagement ping sent, waiting for yes/later
awaiting_platform_select → user chose post/schedule, picking platform
awaiting_schedule_time   → user chose schedule, waiting for date/time
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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


def _send(to: str, body: str, media_url: Optional[str] = None, content_sid: Optional[str] = None) -> None:
    client = _twilio_client()
    kwargs: Dict[str, Any] = {
        "from_": settings.TWILIO_WHATSAPP_FROM,
        "to": f"whatsapp:{to}" if not to.startswith("whatsapp:") else to,
    }
    if content_sid:
        kwargs["content_sid"] = content_sid
    else:
        kwargs["body"] = body
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
    "What would you like to do?\n\n"
    "1️⃣  Post now\n"
    "2️⃣  Schedule post\n"
    "3️⃣  Generate graphic\n"
    "4️⃣  View full caption\n"
    "5️⃣  New idea"
)

GRAPHIC_ACTIONS = (
    "1️⃣  Post graphic now\n"
    "2️⃣  Schedule graphic\n"
    "3️⃣  Download link\n"
    "4️⃣  Edit text\n"
    "5️⃣  Regenerate design"
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

HELP_MESSAGE = (
    "Here's what I can do for you:\n\n"
    "✏️  *Create content* — just tell me what to post about\n"
    "📤  *Post now* — publish to LinkedIn, Instagram, Facebook\n"
    "🗓️  *Schedule post* — pick a date and time\n"
    "💡  *Give me ideas* — 3 content ideas to choose from\n"
    "🎨  *Generate graphic* — create a design for your post\n\n"
    "Or just type a topic and I'll create content for you!"
)

SCHEDULE_PROMPT = (
    "When do you want to post?\n\n"
    "Examples:\n"
    "• *today 5pm*\n"
    "• *tomorrow 9am*\n"
    "• *Monday 3pm*\n"
    "• *18 April 10am*\n\n"
    "_All times are WAT (West Africa Time)_"
)

NO_PLATFORMS = (
    "You don't have any social accounts connected yet.\n\n"
    "Go to your Uri Social dashboard → Settings → Connected Accounts to link "
    "LinkedIn, Instagram, Facebook, and more."
)

# ── Command patterns ──────────────────────────────────────────────────────────

_CREATE_ABOUT = re.compile(
    r"(?:create\s+(?:a\s+)?post\s+about|post\s+about|write\s+(?:a\s+)?post\s+about)\s+(.+)",
    re.IGNORECASE,
)

IDEAS_KEYWORDS = {"give me ideas", "ideas", "idea", "suggestions"}
GRAPHIC_KEYWORDS = {"generate graphic", "graphic", "image", "design"}
CREATE_KEYWORDS = {"create", "create post", "create content", "new post"}
GREETING_KEYWORDS = {"hi", "hello", "hey", "start", "help", "get started", "menu"}
POST_KEYWORDS = {"post", "post now", "publish", "share"}
SCHEDULE_KEYWORDS = {"schedule", "schedule post", "schedule it", "set schedule"}

_EMOJI_DIGITS = {"0️⃣": "0", "1️⃣": "1", "2️⃣": "2", "3️⃣": "3", "4️⃣": "4", "5️⃣": "5", "6️⃣": "6"}

# Network display names
NETWORK_LABELS: Dict[str, str] = {
    "linkedin": "LinkedIn",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "x": "X (Twitter)",
    "twitter": "X (Twitter)",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "pinterest": "Pinterest",
    "threads": "Threads",
}


def _opt(text: str) -> Optional[str]:
    """Normalise a user reply to a plain digit string ("1"–"6") or None."""
    t = text.strip()
    if t in _EMOJI_DIGITS:
        return _EMOJI_DIGITS[t]
    for emoji, digit in _EMOJI_DIGITS.items():
        if t.startswith(emoji):
            return digit
    if t and t[0] in "0123456":
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
        ChatMessage(role="system", content="You are a social media content creator. Return content in the exact format requested."),
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
    from app.domain.models.chat_model import ChatMessage, ChatModel
    from app.services.AIService import AIService

    brand_name = brand.get("brand_name", "")
    industry = brand.get("industry", "")

    prompt = (
        f"Give me 3 punchy, quotable post headlines for {brand_name or 'a brand'}"
        f"{' in the ' + industry + ' space' if industry else ''}.\n"
        f"Topic: {topic or 'anything relevant to the brand'}\n\n"
        "Return ONLY:\n1. [headline]\n2. [headline]\n3. [headline]"
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


# ── Outstand helpers ──────────────────────────────────────────────────────────


async def _get_connected_accounts(user_id: str, db: Optional[AsyncIOMotorDatabase] = None) -> List[Dict[str, Any]]:
    """
    Returns a unified list of connected accounts from both Outstand and direct DB connections.
    Each item has: id, network, name, source ("outstand" | "direct")
    """
    accounts: List[Dict[str, Any]] = []

    # 1. Outstand accounts
    try:
        from app.agents.social_media_manager.services.outstand_service import OutstandService
        outstand = OutstandService()
        result = await outstand.list_accounts(tenant_id=user_id)
        for acc in result.get("data", []):
            acc["source"] = "outstand"
            accounts.append(acc)
    except Exception as e:
        print(f"[WhatsApp] outstand list_accounts error: {e}")

    # 2. Direct DB connections
    if db is not None:
        try:
            cursor = db["social_connections"].find(
                {"user_id": user_id, "connection_status": "active"},
                {"platform": 1, "connected_via": 1, "profile_name": 1, "username": 1}
            )
            async for conn in cursor:
                platform = conn.get("platform", "")
                # Avoid duplicates if already in Outstand list
                already = any(a.get("network") == platform and a.get("source") == "outstand" for a in accounts)
                if not already:
                    accounts.append({
                        "id": str(conn["_id"]),
                        "network": platform,
                        "name": conn.get("account_name") or conn.get("profile_name") or conn.get("username") or platform.title(),
                        "source": "direct",
                        "_user_id": user_id,
                    })
        except Exception as e:
            print(f"[WhatsApp] db connections error: {e}")

    return accounts


def _format_platform_menu(accounts: List[Dict[str, Any]], mode: str = "post") -> str:
    verb = "post to" if mode == "now" else "schedule on"
    lines = [f"Where do you want to {verb}?\n"]
    for i, acc in enumerate(accounts, 1):
        network = acc.get("network", "")
        name = acc.get("name") or acc.get("username") or network
        label = NETWORK_LABELS.get(network, network.title())
        lines.append(f"{i}️⃣  {label} — {name}")
    if len(accounts) > 1:
        lines.append(f"{len(accounts) + 1}️⃣  All platforms")
    lines.append(f"\n0️⃣  Back")
    lines.append("\nReply with the number.")
    return "\n".join(lines)


async def _do_publish(
    accounts: List[Dict[str, Any]],
    caption: str,
    media_url: Optional[str],
    scheduled_at: Optional[str],
    db: Optional[AsyncIOMotorDatabase] = None,
) -> bool:
    """Publishes or schedules via Outstand or direct API. Returns True on success."""
    outstand_ids = [a["id"] for a in accounts if a.get("source") == "outstand"]
    direct_accounts = [a for a in accounts if a.get("source") == "direct"]

    success = True

    # Publish via Outstand
    if outstand_ids:
        try:
            from app.agents.social_media_manager.services.outstand_service import OutstandService
            outstand = OutstandService()
            await outstand.publish_post(
                outstand_account_ids=outstand_ids,
                content=caption,
                scheduled_at=scheduled_at,
                media_urls=[media_url] if media_url else None,
            )
        except Exception as e:
            print(f"[WhatsApp] outstand publish error: {e}")
            success = False

    # Publish via direct connections
    for acc in direct_accounts:
        platform = acc.get("network", "")
        try:
            if platform == "linkedin" and db is not None:
                from app.agents.social_media_manager.services.linkedin_direct_service import LinkedInDirectService
                conn = await db["social_connections"].find_one(
                    {"user_id": acc.get("_user_id"), "platform": "linkedin", "connection_status": "active"}
                )
                token = (conn or {}).get("linkedin_access_token") or (conn or {}).get("access_token")
                urn = (conn or {}).get("person_urn") or (conn or {}).get("active_author_urn")
                if conn and token and urn:
                    svc = LinkedInDirectService()
                    await svc.create_post(access_token=token, person_urn=urn, text=caption)
                else:
                    success = False
            else:
                print(f"[WhatsApp] direct publish not implemented for {platform}")
                success = False
        except Exception as e:
            print(f"[WhatsApp] direct publish error for {platform}: {e}")
            success = False

    return success


# ── Schedule time parser ──────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WAT_OFFSET = timedelta(hours=1)  # UTC+1


def _parse_schedule_time(text: str) -> Optional[datetime]:
    """
    Parse natural language datetime from user. Returns UTC datetime or None.
    Handles: today/tomorrow/weekday + time, or DD Month + time.
    """
    t = text.strip().lower()

    # Extract time component e.g. "3pm", "3:30pm", "15:00"
    time_match = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        t,
        re.IGNORECASE,
    )
    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    ampm = (time_match.group(3) or "").lower()

    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    # Determine the date portion
    now_wat = datetime.now(timezone.utc) + _WAT_OFFSET
    target_date = None

    if "today" in t:
        target_date = now_wat.date()
    elif "tomorrow" in t:
        target_date = (now_wat + timedelta(days=1)).date()
    else:
        # Check weekday names
        for day_name, day_num in _WEEKDAYS.items():
            if day_name in t:
                days_ahead = (day_num - now_wat.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7  # next occurrence
                target_date = (now_wat + timedelta(days=days_ahead)).date()
                break

        if not target_date:
            # Check "DD Month" or "Month DD"
            for month_name, month_num in _MONTHS.items():
                if month_name in t:
                    day_match = re.search(r"\b(\d{1,2})\b", t)
                    if day_match:
                        day_num = int(day_match.group(1))
                        year = now_wat.year
                        try:
                            from datetime import date
                            target_date = date(year, month_num, day_num)
                            if target_date < now_wat.date():
                                target_date = date(year + 1, month_num, day_num)
                        except ValueError:
                            pass
                    break

    if not target_date:
        return None

    # Build WAT datetime then convert to UTC
    wat_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, 0, tzinfo=timezone.utc
    ) - _WAT_OFFSET  # subtract offset to get UTC
    return wat_dt


# ── Safe DB write ─────────────────────────────────────────────────────────────


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
        f'*{headline}*\n'
        f'_{subheadline}_\n\n'
        f"{preview}\n\n"
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

        if state == "awaiting_platform_select":
            await WhatsAppFlowService._handle_platform_select(phone, text, user_id, ctx, db)
            return

        if state == "awaiting_schedule_time":
            await WhatsAppFlowService._handle_schedule_time(phone, body.strip(), user_id, ctx, db)
            return

        # ── Idle / fallback ────────────────────────────────────────────────
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
            f"Hi {first_name} 👋 Welcome to Uri Social!\n\n"
            "I'm your AI content assistant. I'll help you create and publish content to "
            "LinkedIn, Instagram, Facebook, and more — all from WhatsApp.\n\n"
            "Let me create your first piece of content:",
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

        # "post now" / "publish" — post last content if available
        if text in POST_KEYWORDS:
            if ctx.get("caption"):
                await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
            else:
                _send(phone, "No content to post yet. What do you want to post about?")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        # "schedule post"
        if text in SCHEDULE_KEYWORDS:
            if ctx.get("caption"):
                _send(phone, SCHEDULE_PROMPT)
                await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
            else:
                _send(phone, "No content to schedule yet. What do you want to post about?")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        # "create post" without topic
        if text in CREATE_KEYWORDS:
            _send(phone, "What do you want to post about?")
            await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        # Greeting / help
        if text in GREETING_KEYWORDS:
            if text == "help":
                _send(phone, HELP_MESSAGE)
                await _safe_set_state(phone, "idle", ctx, db)
            elif ctx.get("headline"):
                _send(phone, f"Welcome back {first_name} 👋\n\nHere's your last content:\n\n" + _format_content(ctx))
                await _safe_set_state(phone, "showing_content", ctx, db)
            else:
                _send(phone, f"Hi {first_name} 👋\n\n{HELP_MESSAGE}")
            return

        # Anything else — treat as a topic
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

        if opt == "1" or text in POST_KEYWORDS | {"post now"}:
            await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)

        elif opt == "2" or text in SCHEDULE_KEYWORDS:
            _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)

        elif opt == "3" or text in GRAPHIC_KEYWORDS:
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)

        elif opt == "4" or text in {"view full caption", "caption", "full caption"}:
            caption = ctx.get("caption", "No caption saved.")
            _send(phone, f"Full caption:\n\n{caption}\n\n" + CONTENT_ACTIONS)

        elif opt == "5" or text in {"new idea", "change idea", "change", "another"}:
            topic = ctx.get("topic", "")
            await WhatsAppFlowService._create_and_show_content(phone, topic, user_id, ctx, db)

        elif text == "edit":
            _send(phone, EDIT_ACTIONS)
            await _safe_set_state(phone, "awaiting_edit_choice", ctx, db)

        else:
            _send(phone, _format_content(ctx))

    # ── Publish flow ──────────────────────────────────────────────────────────

    @staticmethod
    async def _initiate_post(
        phone: str,
        user_id: str,
        ctx: Dict[str, Any],
        mode: str,  # "now" | "schedule"
        db: AsyncIOMotorDatabase,
    ) -> None:
        """Fetch connected accounts and show platform selection menu."""
        accounts = await _get_connected_accounts(user_id, db)
        if not accounts:
            _send(phone, NO_PLATFORMS)
            await _safe_set_state(phone, "idle", ctx, db)
            return

        menu = _format_platform_menu(accounts, mode=mode)
        _send(phone, menu)
        await _safe_set_state(
            phone,
            "awaiting_platform_select",
            {**ctx, "_accounts": accounts, "_publish_mode": mode},
            db,
        )

    @staticmethod
    async def _handle_platform_select(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        accounts: List[Dict[str, Any]] = ctx.get("_accounts", [])
        mode: str = ctx.get("_publish_mode", "now")

        if not accounts:
            _send(phone, "Something went wrong. Reply *post* to try again.")
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        opt = _opt(text)
        all_opt = str(len(accounts) + 1)

        if text in {"0", "back", "go back", "cancel"}:
            _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        if opt == all_opt:
            selected = accounts
        elif opt and opt.isdigit():
            idx = int(opt) - 1
            if 0 <= idx < len(accounts):
                selected = [accounts[idx]]
            else:
                _send(phone, _format_platform_menu(accounts, mode=mode))
                return
        else:
            _send(phone, _format_platform_menu(accounts, mode=mode))
            return

        caption = ctx.get("caption", "")
        graphic_url = ctx.get("last_graphic_url")

        if mode == "now":
            _send(phone, "Publishing... 🚀")
            success = await _do_publish(selected, caption, graphic_url, scheduled_at=None, db=db)
            if success:
                platform_names = ", ".join(
                    NETWORK_LABELS.get(acc.get("network", ""), acc.get("network", "")) for acc in selected
                )
                _send(phone, f"✅ Posted to {platform_names}!\n\nWhat's next?\n\n" + CONTENT_ACTIONS)
                await _safe_set_state(phone, "showing_content", ctx, db)
            else:
                _send(phone, "❌ Could not publish right now. Please try again.\n\n" + CONTENT_ACTIONS)
                await _safe_set_state(phone, "showing_content", ctx, db)
        else:
            # Schedule mode — store selected accounts, ask for time
            _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(
                phone,
                "awaiting_schedule_time",
                {**ctx, "_schedule_accounts": selected},
                db,
            )

    @staticmethod
    async def _handle_schedule_time(
        phone: str,
        raw_body: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        scheduled_dt = _parse_schedule_time(raw_body)

        if not scheduled_dt:
            _send(
                phone,
                "I couldn't understand that time. Please try:\n\n"
                "• *today 5pm*\n• *tomorrow 9am*\n• *Monday 3pm*\n• *18 April 10am*"
            )
            return

        # Check if we already have the platform selected
        schedule_accounts: List[Dict[str, Any]] = ctx.get("_schedule_accounts", [])

        if not schedule_accounts:
            # Platform not yet selected — store time then ask for platform
            await _safe_set_state(
                phone,
                "awaiting_platform_select",
                {**ctx, "_scheduled_at": scheduled_dt.isoformat(), "_publish_mode": "schedule"},
                db,
            )
            accounts = await _get_connected_accounts(user_id, db)
            if not accounts:
                _send(phone, NO_PLATFORMS)
                await _safe_set_state(phone, "idle", ctx, db)
                return
            new_ctx = {**ctx, "_accounts": accounts, "_publish_mode": "schedule", "_scheduled_at": scheduled_dt.isoformat()}
            await _safe_set_state(phone, "awaiting_platform_select", new_ctx, db)
            _send(phone, _format_platform_menu(accounts, mode="schedule"))
            return

        # Platform already selected — schedule now
        caption = ctx.get("caption", "")
        graphic_url = ctx.get("last_graphic_url")
        _send(phone, "Scheduling your post... 🗓️")

        success = await _do_publish(schedule_accounts, caption, graphic_url, scheduled_at=scheduled_dt.isoformat(), db=db)

        if success:
            # Format time back in WAT for user display
            wat_dt = scheduled_dt + _WAT_OFFSET
            time_str = wat_dt.strftime("%A, %d %B at %-I:%M %p") + " WAT"
            platform_names = ", ".join(
                NETWORK_LABELS.get(acc.get("network", ""), acc.get("network", "")) for acc in schedule_accounts
            )
            _send(
                phone,
                f"✅ Scheduled for {time_str}\n"
                f"Platform: {platform_names}\n\n"
                "What would you like to do next?\n\n"
                + CONTENT_ACTIONS,
            )
            await _safe_set_state(phone, "showing_content", ctx, db)
        else:
            _send(phone, "❌ Could not schedule right now. Please try again.\n\n" + CONTENT_ACTIONS)
            await _safe_set_state(phone, "showing_content", ctx, db)

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
        await _safe_set_state(phone, "showing_ideas", {"ideas": ideas, "topic": topic}, db)

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
        await _safe_set_state(phone, "awaiting_edit_value", {**ctx, "edit_field": field}, db)

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
            new_ctx = {**ctx, "tone": value}
            await WhatsAppFlowService._create_and_show_content(
                phone, ctx.get("topic", value), user_id, new_ctx, db
            )
        else:
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
            except Exception as e:
                print(f"[WhatsApp] imgBB upload error: {e}")
        elif raw_url.startswith("http"):
            public_url = raw_url

        if public_url:
            try:
                _send(phone, "Your design is ready 👆", media_url=public_url)
            except Exception as e:
                print(f"[WhatsApp] Twilio media send failed: {e}")
                _send(phone, f"Your design is ready 👆\n\n🔗 {public_url}")
            _send(phone, GRAPHIC_ACTIONS)
        else:
            _send(phone, "Your design is ready 👆\n\nPreview unavailable. Check your Uri Social dashboard.")
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

        if opt == "1" or text in {"post graphic", "post graphic now", "post now"}:
            await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)

        elif opt == "2" or text in {"schedule graphic", "schedule"}:
            _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)

        elif opt == "3" or text == "download":
            url = ctx.get("last_graphic_url")
            if url:
                _send(phone, f"🔗 Download your graphic:\n{url}")
            else:
                _send(phone, "Link unavailable. Generate a new graphic below.")
            _send(phone, GRAPHIC_ACTIONS)

        elif opt == "4" or text in {"edit text", "edit"}:
            _send(phone, EDIT_ACTIONS)
            await _safe_set_state(phone, "awaiting_edit_choice", ctx, db)

        elif opt == "5" or text in {"regenerate", "regenerate design", "new design"}:
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

                if last_updated and state == "idle":
                    delta = datetime.now(timezone.utc).replace(tzinfo=None) - last_updated
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

                _send(phone, f"Good morning {first_name} 👋\n\nYour content for today is ready.")
                _send(phone, _format_content(new_ctx))
                await _safe_set_state(phone, "showing_content", new_ctx, db)
                sent += 1

            except Exception as e:
                print(f"Daily push failed for {phone}: {e}")
                failed += 1

        return {"sent": sent, "failed": failed, "total": len(users)}
