"""
WhatsApp conversation flow service — Uri Social assistant.

Design principle: Conversational AI. No numbered menus.
Users say what they want in plain language; AI classifies intent when needed.

States
------
linked                      → first-time user — greet and ask what they want
idle                        → returning user waiting for a command
showing_content             → content displayed, awaiting next action
showing_ideas               → 3 ideas shown, awaiting pick
awaiting_topic              → asked "what do you want to post about?"
awaiting_edit_choice        → asked what to edit
awaiting_edit_value         → waiting for the replacement text / tone
showing_graphic             → graphic shown, awaiting next action
awaiting_re_engagement      → re-engagement ping sent, waiting for yes/later
awaiting_platform_select    → picking platform to post/schedule on
awaiting_schedule_time      → user chose schedule, waiting for date/time
awaiting_publish_confirm    → preview shown, waiting for yes / edit / cancel
"""

from __future__ import annotations

import asyncio
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


async def _send(to: str, body: str, media_url: Optional[str] = None, content_sid: Optional[str] = None) -> None:
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
    await asyncio.to_thread(client.messages.create, **kwargs)


# ── Credit helper ─────────────────────────────────────────────────────────────


async def _check_and_deduct_credit(user_id: str, reason: str) -> bool:
    """
    Check the user has at least 1 credit, then deduct it.
    Mirrors the pattern used in complete_social_manager.py.
    Returns True if the action is allowed (credit deducted or legacy user).
    Returns False if the user has no credits remaining.
    On any credit-system error, allows the action (fail open).
    """
    try:
        import uuid
        from app.services.CreditService import credit_service
        from app.services.TrialService import trial_service

        is_trial = await trial_service.has_active_trial(user_id)

        if not is_trial:
            has_credits = await credit_service.check_sufficient_credits(user_id)
            if not has_credits:
                return False

        ref_id = str(uuid.uuid4())[:12]
        if is_trial:
            await trial_service.deduct_trial_credit(
                user_id=user_id,
                campaign_id=ref_id,
                reason=reason,
            )
        else:
            await credit_service.deduct_credit(
                user_id=user_id,
                campaign_id=ref_id,
                reason=reason,
                retry_count=0,
            )
        print(f"[WhatsApp] 1 credit deducted from user={user_id} reason={reason}")
        return True
    except Exception as e:
        print(f"[WhatsApp] credit system error (allowing action): {e}")
        return True  # fail open — never block user due to billing bugs


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
    "I can *post it now*, *schedule it*, *make a graphic*, give you a *new idea*, or *edit* this — just say the word! 🎯"
)

CAPABILITIES = (
    "Here's what I can do:\n\n"
    "✏️  *Create a post* — just tell me what to post about\n"
    "📤  *Post it now* — publish to LinkedIn, Instagram, Facebook, and more\n"
    "🗓️  *Schedule it* — pick a date and time\n"
    "💡  *Give me ideas* — 3 headlines to pick from\n"
    "🎨  *Make a graphic* — design an image for your post\n"
    "✏️  *Edit* — tweak the headline, tone, or caption\n\n"
    "What would you like to do?"
)

GRAPHIC_ACTIONS = (
    "What's next?\n\n"
    "I can *post it*, *schedule it*, let you *download* it, *edit* the design, or try a *new design* — just say the word!\n"
    "Say *back* to return to your content."
)

RE_ENGAGEMENT = (
    "We've got fresh content ideas for you! 🎉\n\n"
    "Want to see them? Reply *yes* or *later*"
)

HELP_MESSAGE = CAPABILITIES

SCHEDULE_PROMPT = (
    "When do you want to post?\n\n"
    "Examples:\n"
    "• *today 5pm*\n"
    "• *tomorrow 9am*\n"
    "• *Monday 3pm*\n"
    "• *18 April 10am*\n\n"
    "_All times are WAT (West Africa Time)_\n\n"
    "Reply *back* to cancel."
)

NO_PLATFORMS = (
    "You don't have any social accounts connected yet.\n\n"
    "Go to your Uri Social dashboard → Settings → Connected Accounts to link "
    "LinkedIn, Instagram, Facebook, and more."
)

# ── Intent keyword sets ───────────────────────────────────────────────────────

_POST_NOW_WORDS = {
    "post", "post now", "post it", "post it now", "publish", "publish it",
    "share", "share it", "send it", "go ahead", "do it", "live", "go live",
    "put it out", "push it", "drop it",
}
_SCHEDULE_WORDS = {
    "schedule", "schedule it", "schedule post", "later", "plan", "plan it",
    "set time", "add to schedule", "set a time", "delay", "queue", "queue it",
    "schedule for", "post later",
}
_GRAPHIC_WORDS = {
    "graphic", "image", "design", "visual", "picture", "photo",
    "create graphic", "make graphic", "generate graphic", "make image",
    "create image", "make design", "generate image", "make a graphic",
    "make a design", "make an image", "create a graphic", "add image",
}
_NEW_IDEA_WORDS = {
    "new idea", "another idea", "different idea", "change idea", "try again",
    "something else", "next", "other idea", "new content", "redo", "another",
    "different topic", "new topic", "new one", "try another", "different one",
    "different", "refresh", "new post",
}
_EDIT_WORDS = {
    "edit", "change it", "modify", "update", "rewrite", "fix", "adjust",
    "tweak", "amend", "alter", "revise", "improve",
}
_CAPTION_WORDS = {
    "caption", "full caption", "view caption", "show caption", "read more",
    "see caption", "show me the caption", "read the caption", "view full",
    "show full", "full text",
}
_BACK_WORDS = {"back", "go back", "cancel", "return", "stop", "quit", "exit", "nevermind", "never mind"}

_GREETING_WORDS = {
    "hey", "hi", "hello", "hiya", "howdy", "sup", "what's up", "whats up",
    "yo", "good morning", "good afternoon", "good evening", "morning", "afternoon",
    "evening", "hi there", "hey there", "greetings", "helo", "hii", "heya",
}

# Ordinals for picking ideas / platforms
_ORDINALS: Dict[str, int] = {
    "1": 0, "first": 0, "1st": 0, "one": 0, "the first": 0, "first one": 0,
    "2": 1, "second": 1, "2nd": 1, "two": 1, "the second": 1, "second one": 1,
    "3": 2, "third": 2, "3rd": 2, "three": 2, "the third": 2, "third one": 2,
}

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

# ── AI intent classifier ───────────────────────────────────────────────────────


async def _ai_intent(text: str, options: List[str], context_hint: str = "") -> str:
    """
    Classify `text` into one of `options` using GPT-4o-mini.
    Only called when keyword matching fails. Returns one of the option strings.
    """
    from app.domain.models.chat_model import ChatMessage, ChatModel
    from app.services.AIService import AIService

    opts_str = " | ".join(options)
    system = (
        f"You are an intent classifier for a WhatsApp social media assistant. "
        f"Classify the user's message into exactly one of these intents: {opts_str}. "
        f"{context_hint}"
        "Reply with ONLY the intent label — nothing else."
    )
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=text),
    ]
    req = ChatModel(model="gpt-4o-mini", messages=messages, temperature=0)
    try:
        result = await AIService.chat_completion(req)
        if isinstance(result, dict) and result.get("error"):
            return "unknown"
        raw = result.choices[0].message.content.strip().lower()
        # Return first token to guard against verbose responses
        return raw.split()[0] if raw else "unknown"
    except Exception as e:
        print(f"[WhatsApp] _ai_intent error: {e}")
        return "unknown"


# ── Platform name matcher ─────────────────────────────────────────────────────


def _match_platform_by_name(text: str, accounts: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """
    Return a list of matched accounts if the user named a platform or said 'all'.
    Returns None if no match found.
    """
    t = text.lower()

    # "all" → every platform
    if any(w in t for w in ("all", "every", "everywhere", "all platforms", "all of them")):
        return accounts

    matched = []
    for acc in accounts:
        network = acc.get("network", "").lower()
        label = NETWORK_LABELS.get(network, network).lower()
        name = (acc.get("name") or acc.get("username") or "").lower()
        if network in t or label in t or (name and name in t):
            matched.append(acc)

    return matched if matched else None


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
    req = ChatModel(model="gpt-4o-mini", messages=messages, temperature=0.8)
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
    req = ChatModel(model="gpt-4o-mini", messages=messages, temperature=0.9)
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
    accounts: List[Dict[str, Any]] = []

    try:
        from app.agents.social_media_manager.services.outstand_service import OutstandService
        outstand = OutstandService()
        result = await outstand.list_accounts(tenant_id=user_id)
        for acc in result.get("data", []):
            acc["source"] = "outstand"
            accounts.append(acc)
    except Exception as e:
        print(f"[WhatsApp] outstand list_accounts error: {e}")

    if db is not None:
        try:
            cursor = db["social_connections"].find(
                {"user_id": user_id, "connection_status": "active"},
                {"platform": 1, "connected_via": 1, "profile_name": 1, "username": 1, "account_name": 1}
            )
            async for conn in cursor:
                platform = conn.get("platform", "")
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
    for acc in accounts:
        network = acc.get("network", "")
        name = acc.get("name") or acc.get("username") or network
        label = NETWORK_LABELS.get(network, network.title())
        lines.append(f"• {label} — {name}")
    if len(accounts) > 1:
        lines.append("• All platforms")
    lines.append("\nJust say the platform name, or *all* for everything. Say *back* to cancel.")
    return "\n".join(lines)


async def _do_publish(
    accounts: List[Dict[str, Any]],
    caption: str,
    media_url: Optional[str],
    scheduled_at: Optional[str],
    db: Optional[AsyncIOMotorDatabase] = None,
) -> bool:
    outstand_ids = [a["id"] for a in accounts if a.get("source") == "outstand"]
    direct_accounts = [a for a in accounts if a.get("source") == "direct"]

    success = True

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
                    await svc.create_post(access_token=token, person_urn=urn, text=caption, image_url=media_url)
                else:
                    print(f"[WhatsApp] LinkedIn direct missing token or URN for user={acc.get('_user_id')}")
                    success = False

            elif platform == "instagram" and db is not None:
                from app.agents.social_media_manager.services.instagram_direct_service import InstagramDirectService
                conn = await db["social_connections"].find_one(
                    {"user_id": acc.get("_user_id"), "platform": "instagram",
                     "connected_via": {"$in": ["instagram_direct", "instagram_direct_oauth"]},
                     "connection_status": "active"}
                )
                ig_user_id = (conn or {}).get("ig_user_id")
                page_token = (conn or {}).get("page_access_token")
                page_id = (conn or {}).get("page_id")
                if conn and ig_user_id and page_token:
                    if scheduled_at:
                        print(f"[WhatsApp] Instagram scheduling deferred to cron for {scheduled_at}")
                    else:
                        result = await InstagramDirectService.publish_post(
                            ig_user_id=ig_user_id,
                            page_access_token=page_token,
                            content=caption,
                            image_url=media_url or None,
                            page_id=page_id,
                        )
                        if not result.get("success"):
                            print(f"[WhatsApp] Instagram publish failed: {result.get('error')}")
                            success = False
                else:
                    print(f"[WhatsApp] Instagram direct missing credentials for user={acc.get('_user_id')}")
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
    t = text.strip().lower()

    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t, re.IGNORECASE)
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

    now_wat = datetime.now(timezone.utc) + _WAT_OFFSET
    target_date = None

    if "today" in t:
        target_date = now_wat.date()
    elif "tomorrow" in t:
        target_date = (now_wat + timedelta(days=1)).date()
    else:
        for day_name, day_num in _WEEKDAYS.items():
            if day_name in t:
                days_ahead = (day_num - now_wat.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                target_date = (now_wat + timedelta(days=days_ahead)).date()
                break

        if not target_date:
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

    wat_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, 0, tzinfo=timezone.utc
    ) - _WAT_OFFSET
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
        "What would you like to do?\n\n"
        + CONTENT_ACTIONS
    )


# ── Main dispatcher ───────────────────────────────────────────────────────────


class WhatsAppFlowService:

    @staticmethod
    async def handle(raw_from: str, body: str, db: AsyncIOMotorDatabase) -> None:
        phone = WhatsAppSessionService._normalize_phone(raw_from)
        try:
            await WhatsAppFlowService._handle_inner(phone, body, db)
        except Exception as exc:
            import traceback
            print(f"[WhatsApp] ❌ UNHANDLED EXCEPTION for phone={phone!r}: {exc}\n{traceback.format_exc()}")
            try:
                await _safe_set_state(phone, "idle", {}, db)
                await _send(phone, "Something went wrong on our end. Just tell me what you want to do and I'll sort it out.")
            except Exception:
                pass

    @staticmethod
    async def _handle_inner(phone: str, body: str, db: AsyncIOMotorDatabase) -> None:
        text = body.strip().lower()

        user = await WhatsAppSessionService.get_user_by_phone(phone, db)
        if not user:
            print(f"[WhatsApp] no user found for phone={phone!r}")
            await _send(phone, NOT_LINKED)
            return

        user_id: str = user["userId"]
        first_name: str = user.get("first_name") or "there"

        session = await WhatsAppSessionService.get_session(phone, db) or {}
        state: str = session.get("state", "linked")
        ctx: Dict[str, Any] = session.get("context", {})

        # ── Global reset — works from any state ───────────────────────────
        _RESET = {"restart", "reset", "menu", "home", "start over", "start fresh", "main menu"}
        if text in _RESET:
            await _send(phone, f"No problem {first_name}! Here's what I can do:\n\n" + HELP_MESSAGE)
            await _safe_set_state(phone, "idle", {}, db)
            return

        # ── First-time user ────────────────────────────────────────────────
        if state == "linked":
            await WhatsAppFlowService._first_time_entry(phone, first_name, user_id, db)
            return
        if state == "awaiting_topic":
            if text in _BACK_WORDS:
                if ctx.get("headline"):
                    await _send(phone, _format_content(ctx))
                    await _safe_set_state(phone, "showing_content", ctx, db)
                else:
                    await _send(phone, HELP_MESSAGE)
                    await _safe_set_state(phone, "idle", ctx, db)
                return
            await WhatsAppFlowService._create_and_show_content(phone, body.strip(), user_id, ctx, db)
            return

        if state == "awaiting_edit_choice":
            await WhatsAppFlowService._handle_edit_choice(phone, text, body.strip(), user_id, ctx, db)
            return

        if state == "awaiting_edit_value":
            if text in _BACK_WORDS:
                await _send(phone, _format_content(ctx))
                await _safe_set_state(phone, "showing_content", ctx, db)
                return
            await WhatsAppFlowService._apply_edit(phone, body.strip(), user_id, ctx, db)
            return

        if state == "showing_content":
            await WhatsAppFlowService._handle_content_actions(phone, text, body.strip(), user_id, ctx, db)
            return

        if state == "showing_ideas":
            await WhatsAppFlowService._handle_ideas_pick(phone, text, body.strip(), user_id, ctx, db)
            return

        if state == "generating_graphic":
            await _send(phone, "⏳ Still working on your graphic — it takes 2-3 minutes. I'll send it as soon as it's ready!")
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

        if state == "awaiting_publish_confirm":
            await WhatsAppFlowService._handle_publish_confirm(phone, text, user_id, ctx, db)
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
            await _send(phone, f"Hi {first_name} 👋\n\nYour Uri Social account is ready.\n\n" + NO_BRAND)
            await _safe_set_state(phone, "idle", {}, db)
            return

        await _send(
            phone,
            f"Hey {first_name}! 👋 Welcome to Uri Social!\n\n"
            "I'm your AI social media assistant. I can help you create posts, generate graphics, "
            "schedule content, and publish to LinkedIn, Instagram, Facebook — all from WhatsApp.\n\n"
            + CAPABILITIES,
        )
        await _safe_set_state(phone, "idle", {}, db)

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
        # ── Greetings — respond warmly, show capabilities, do NOT generate content ──
        if text in _GREETING_WORDS or text in {"start", "help", "help me", "get started", "menu"}:
            if ctx.get("headline"):
                await _send(
                    phone,
                    f"Hey {first_name}! 👋 Welcome back.\n\n"
                    "You've got content ready to go:\n\n"
                    + _format_content(ctx),
                )
                await _safe_set_state(phone, "showing_content", ctx, db)
            else:
                await _send(phone, f"Hey {first_name}! 👋\n\n" + CAPABILITIES)
            return

        # "create post about [topic]" pattern
        m = re.match(
            r"(?:create\s+(?:a\s+)?post\s+about|post\s+about|write\s+(?:a\s+)?post\s+about)\s+(.+)",
            raw_body, re.IGNORECASE,
        )
        if m:
            await WhatsAppFlowService._create_and_show_content(phone, m.group(1).strip(), user_id, {}, db)
            return

        if any(w in text for w in ("give me ideas", "ideas", "idea", "suggestions", "what should i post")):
            await WhatsAppFlowService._send_ideas(phone, user_id, "", db)
            return

        if any(w in text for w in _GRAPHIC_WORDS) and ctx.get("headline"):
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
            return

        if any(w in text for w in _POST_NOW_WORDS):
            if ctx.get("caption"):
                await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
            else:
                await _send(phone, "What do you want to post about?")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        if any(w in text for w in _SCHEDULE_WORDS):
            if ctx.get("caption"):
                await _send(phone, SCHEDULE_PROMPT)
                await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
            else:
                await _send(phone, "What do you want to schedule? Tell me the topic first.")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        if text in {"create", "create post", "create content", "new post", "write a post", "write post"}:
            await _send(phone, "What do you want to post about?")
            await _safe_set_state(phone, "awaiting_topic", ctx, db)
            return

        # Use AI to decide: is this a topic, or an ambiguous command?
        intent = await _ai_intent(
            raw_body,
            ["create_content", "post_now", "schedule", "graphic", "ideas", "edit", "greeting", "unknown"],
            "The user is idle. They have not yet seen any content this session.",
        )

        if intent == "create_content":
            await WhatsAppFlowService._create_and_show_content(phone, raw_body, user_id, {}, db)
        elif intent == "post_now":
            if ctx.get("caption"):
                await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
            else:
                await _send(phone, "What do you want to post about? Tell me the topic and I'll write it.")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
        elif intent == "schedule":
            if ctx.get("caption"):
                await _send(phone, SCHEDULE_PROMPT)
                await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
            else:
                await _send(phone, "What do you want to schedule? Give me the topic first.")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
        elif intent == "graphic":
            if ctx.get("headline"):
                await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
            else:
                await _send(phone, "I'll need to create some content first before I can make a graphic. What topic should the post be about?")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
        elif intent == "ideas":
            await WhatsAppFlowService._send_ideas(phone, user_id, "", db)
        elif intent == "edit":
            if ctx.get("headline"):
                await WhatsAppFlowService._handle_edit_choice(phone, text, raw_body, user_id, ctx, db)
            else:
                await _send(phone, "There's no content to edit yet. Tell me what to post about and I'll create something.")
                await _safe_set_state(phone, "awaiting_topic", ctx, db)
        elif intent == "greeting":
            await _send(phone, f"Hey {first_name}! 👋\n\n" + CAPABILITIES)
        else:
            # Truly unknown — ask for clarification instead of guessing
            await _send(
                phone,
                f"I'm not sure what you'd like me to do. 🤔\n\n" + CAPABILITIES,
            )

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
            await _send(phone, NO_BRAND)
            await _safe_set_state(phone, "idle", {}, db)
            return

        # Credit check — 1 credit per content generation
        allowed = await _check_and_deduct_credit(user_id, reason="whatsapp_content_generation")
        if not allowed:
            await _send(
                phone,
                "⚠️ You've run out of credits.\n\n"
                "Upgrade your plan on the Uri Social dashboard to keep creating content."
            )
            await _safe_set_state(phone, "idle", ctx, db)
            return

        await _send(phone, "Creating your content... ✍️")

        tone = ctx.get("tone")
        content = await _generate_content_structured(topic, brand, tone=tone)
        if not content:
            await _send(phone, "Could not generate content right now. Please try again.")
            await _safe_set_state(phone, "idle", {}, db)
            return

        new_ctx = {
            "topic": topic,
            "headline": content["headline"],
            "subheadline": content["subheadline"],
            "caption": content["caption"],
            "tone": tone,
        }

        await _send(phone, _format_content(new_ctx))
        await _safe_set_state(phone, "showing_content", new_ctx, db)

    # ── Content action handler (state: showing_content) ───────────────────────

    @staticmethod
    async def _handle_content_actions(
        phone: str,
        text: str,
        raw_body: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        # If user sends a greeting in this state, re-show content + options
        if text in _GREETING_WORDS:
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        # Fast-path keyword matching
        # ⚠️ Check graphic FIRST — "generate a graphic for this post" contains "post"
        if any(w in text for w in _GRAPHIC_WORDS) or text == "3":
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
            return

        if any(w in text for w in _POST_NOW_WORDS) or text == "1":
            await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
            return

        if any(w in text for w in _SCHEDULE_WORDS) or text == "2":
            await _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
            return

        if any(w in text for w in _CAPTION_WORDS) or text == "4":
            caption = ctx.get("caption", "No caption saved.")
            await _send(phone, f"Full caption:\n\n{caption}\n\n" + CONTENT_ACTIONS)
            return

        if any(w in text for w in _NEW_IDEA_WORDS) or text == "5":
            topic = ctx.get("topic", "")
            await WhatsAppFlowService._create_and_show_content(phone, topic, user_id, ctx, db)
            return

        if any(w in text for w in _EDIT_WORDS) or text == "edit":
            # Check if they specified what to edit inline, e.g. "make it funnier" or "change the headline to X"
            await WhatsAppFlowService._handle_edit_choice(phone, text, raw_body, user_id, ctx, db)
            return

        if any(w in text for w in ("ideas", "give me ideas", "idea", "suggestions")):
            await WhatsAppFlowService._send_ideas(phone, user_id, ctx.get("topic", ""), db)
            return

        # AI intent fallback
        intent = await _ai_intent(
            raw_body,
            ["post_now", "schedule", "graphic", "caption", "new_idea", "edit", "ideas", "greeting", "unknown"],
            "The user has just seen social media content (headline, subheadline, caption).",
        )

        if intent == "post_now":
            await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
        elif intent == "schedule":
            await _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
        elif intent == "graphic":
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
        elif intent == "caption":
            caption = ctx.get("caption", "No caption saved.")
            await _send(phone, f"Full caption:\n\n{caption}\n\n" + CONTENT_ACTIONS)
        elif intent == "new_idea":
            topic = ctx.get("topic", "")
            await WhatsAppFlowService._create_and_show_content(phone, topic, user_id, ctx, db)
        elif intent == "edit":
            await WhatsAppFlowService._handle_edit_choice(phone, text, raw_body, user_id, ctx, db)
        elif intent == "ideas":
            await WhatsAppFlowService._send_ideas(phone, user_id, ctx.get("topic", ""), db)
        elif intent == "greeting":
            # Re-show the content so they can pick an action
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
        else:
            # Unknown — ask for clarification rather than taking an unsolicited action
            await _send(
                phone,
                "I'm not sure what you'd like me to do with this content. 🤔\n\n"
                "What would you like to do?\n\n"
                + CONTENT_ACTIONS,
            )

    # ── Publish flow ──────────────────────────────────────────────────────────

    @staticmethod
    async def _initiate_post(
        phone: str,
        user_id: str,
        ctx: Dict[str, Any],
        mode: str,
        db: AsyncIOMotorDatabase,
    ) -> None:
        accounts = await _get_connected_accounts(user_id, db)
        if not accounts:
            await _send(phone, NO_PLATFORMS)
            await _safe_set_state(phone, "idle", ctx, db)
            return

        # If only one platform, skip the platform selection step
        if len(accounts) == 1:
            selected = accounts
        else:
            # Multiple platforms — ask which one first, then confirm
            menu = _format_platform_menu(accounts, mode=mode)
            await _send(phone, menu)
            await _safe_set_state(
                phone,
                "awaiting_platform_select",
                {**ctx, "_accounts": accounts, "_publish_mode": mode},
                db,
            )
            return

        # Show confirmation preview before posting
        caption = ctx.get("caption", "")
        headline = ctx.get("headline", "")
        graphic_url = ctx.get("last_graphic_url")
        platform_names = ", ".join(
            NETWORK_LABELS.get(acc.get("network", ""), acc.get("network", "")) for acc in selected
        )
        image_line = "\n🖼️ *Graphic attached*" if graphic_url else ""
        await _send(
            phone,
            f"Here's what will be posted to *{platform_names}*:\n\n"
            f"*{headline}*\n\n"
            f"{caption}{image_line}\n\n"
            "Ready to post this? Reply *yes* to confirm, *edit* to make changes, or *cancel* to go back.",
        )
        await _safe_set_state(
            phone,
            "awaiting_publish_confirm",
            {**ctx, "_confirm_accounts": selected, "_confirm_mode": mode},
            db,
        )

    @staticmethod
    async def _publish_to(
        phone: str,
        selected: List[Dict[str, Any]],
        caption: str,
        graphic_url: Optional[str],
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        try:
            await _send(phone, "Publishing... 🚀")
        except Exception as e:
            print(f"[WhatsApp] failed to send 'Publishing...' message: {e}")
        try:
            success = await _do_publish(selected, caption, graphic_url, scheduled_at=None, db=db)
        except Exception as e:
            print(f"[WhatsApp] _do_publish raised: {e}")
            success = False
        try:
            if success:
                platform_names = ", ".join(
                    NETWORK_LABELS.get(acc.get("network", ""), acc.get("network", "")) for acc in selected
                )
                await _send(phone, f"✅ Posted to {platform_names}!\n\nWhat's next?\n\n" + CONTENT_ACTIONS)
            else:
                await _send(phone, "❌ Could not publish right now. Please try again.\n\n" + CONTENT_ACTIONS)
        except Exception as e:
            print(f"[WhatsApp] failed to send publish confirmation: {e}")
        await _safe_set_state(phone, "showing_content", ctx, db)

    @staticmethod
    async def _handle_publish_confirm(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        """State: awaiting_publish_confirm — user sees a preview and must say yes/edit/cancel."""
        selected: List[Dict[str, Any]] = ctx.get("_confirm_accounts", [])
        mode: str = ctx.get("_confirm_mode", "now")
        caption = ctx.get("caption", "")
        graphic_url = ctx.get("last_graphic_url")

        _YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "go", "do it", "post it", "publish"}

        if text in _YES_WORDS:
            if mode == "now":
                await WhatsAppFlowService._publish_to(phone, selected, caption, graphic_url, ctx, db)
            else:
                await _send(phone, SCHEDULE_PROMPT)
                await _safe_set_state(phone, "awaiting_schedule_time", {**ctx, "_schedule_accounts": selected}, db)
            return

        if any(w in text for w in _EDIT_WORDS):
            await WhatsAppFlowService._handle_edit_choice(phone, text, text, user_id, ctx, db)
            return

        if text in _BACK_WORDS or text == "cancel":
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        # Anything else — remind them what they need to confirm
        await _send(
            phone,
            "Just reply *yes* to post, *edit* to make changes, or *cancel* to go back."
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
            await _send(phone, "Something went wrong. Tell me where you want to post and I'll try again.")
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        if text in _BACK_WORDS:
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        # Try natural language platform name matching
        selected = _match_platform_by_name(text, accounts)

        # Fall back to index number if no name match
        if not selected:
            m = re.search(r"\b(\d+)\b", text)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(accounts):
                    selected = [accounts[idx]]
                elif idx == len(accounts):  # "all" by number
                    selected = accounts

        if not selected:
            await _send(phone, _format_platform_menu(accounts, mode=mode))
            return

        caption = ctx.get("caption", "")
        headline = ctx.get("headline", "")
        graphic_url = ctx.get("last_graphic_url")

        if mode == "now":
            # Show confirmation preview before posting
            platform_names = ", ".join(
                NETWORK_LABELS.get(acc.get("network", ""), acc.get("network", "")) for acc in selected
            )
            image_line = "\n🖼️ *Graphic attached*" if graphic_url else ""
            await _send(
                phone,
                f"Here's what will be posted to *{platform_names}*:\n\n"
                f"*{headline}*\n\n"
                f"{caption}{image_line}\n\n"
                "Ready to post this? Reply *yes* to confirm, *edit* to make changes, or *cancel* to go back.",
            )
            await _safe_set_state(
                phone,
                "awaiting_publish_confirm",
                {**ctx, "_confirm_accounts": selected, "_confirm_mode": "now"},
                db,
            )
        else:
            await _send(phone, SCHEDULE_PROMPT)
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
        if raw_body.strip().lower() in _BACK_WORDS:
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        scheduled_dt = _parse_schedule_time(raw_body)

        if not scheduled_dt:
            await _send(
                phone,
                "I couldn't understand that time. Try something like:\n\n"
                "• *today 5pm*\n• *tomorrow 9am*\n• *Monday 3pm*\n• *18 April 10am*\n\n"
                "Or say *back* to return."
            )
            return

        schedule_accounts: List[Dict[str, Any]] = ctx.get("_schedule_accounts", [])

        if not schedule_accounts:
            accounts = await _get_connected_accounts(user_id, db)
            if not accounts:
                await _send(phone, NO_PLATFORMS)
                await _safe_set_state(phone, "idle", ctx, db)
                return
            new_ctx = {**ctx, "_accounts": accounts, "_publish_mode": "schedule", "_scheduled_at": scheduled_dt.isoformat()}
            await _safe_set_state(phone, "awaiting_platform_select", new_ctx, db)
            await _send(phone, _format_platform_menu(accounts, mode="schedule"))
            return

        caption = ctx.get("caption", "")
        graphic_url = ctx.get("last_graphic_url")
        await _send(phone, "Scheduling your post... 🗓️")

        success = await _do_publish(schedule_accounts, caption, graphic_url, scheduled_at=scheduled_dt.isoformat(), db=db)

        if success:
            wat_dt = scheduled_dt + _WAT_OFFSET
            time_str = wat_dt.strftime("%A, %d %B at %-I:%M %p") + " WAT"
            platform_names = ", ".join(
                NETWORK_LABELS.get(acc.get("network", ""), acc.get("network", "")) for acc in schedule_accounts
            )
            await _send(
                phone,
                f"✅ Scheduled for {time_str}\n"
                f"Platform: {platform_names}\n\n"
                "What would you like to do next?\n\n"
                + CONTENT_ACTIONS,
            )
            await _safe_set_state(phone, "showing_content", ctx, db)
        else:
            await _send(phone, "❌ Could not schedule right now. Please try again.\n\n" + CONTENT_ACTIONS)
            await _safe_set_state(phone, "showing_content", ctx, db)

    # ── Ideas flow ────────────────────────────────────────────────────────────

    @staticmethod
    async def _send_ideas(
        phone: str, user_id: str, topic: str, db: AsyncIOMotorDatabase
    ) -> None:
        brand = await _brand_context(user_id, db)
        if not brand:
            await _send(phone, NO_BRAND)
            await _safe_set_state(phone, "idle", {}, db)
            return

        ideas = await _generate_three_ideas(topic, brand)
        if not ideas:
            await _send(phone, "Could not generate ideas right now. Try again shortly.")
            return

        lines = "\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(ideas))
        await _send(
            phone,
            f"Here are 3 ideas for you:\n\n{lines}\n\n"
            "Which one do you like? Say *first*, *second*, *third*, or just the number."
        )
        await _safe_set_state(phone, "showing_ideas", {"ideas": ideas, "topic": topic}, db)

    @staticmethod
    async def _handle_ideas_pick(
        phone: str,
        text: str,
        raw_body: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        ideas: List[str] = ctx.get("ideas", [])

        # ── Back / none — return to content or idle ───────────────────────────
        _NONE_WORDS = {"none", "none of them", "neither", "not interested", "no thanks", "nope", "no"}
        if any(w in text for w in _BACK_WORDS) or any(w in text for w in _NONE_WORDS):
            if ctx.get("headline"):
                await _send(phone, _format_content(ctx))
                await _safe_set_state(phone, "showing_content", ctx, db)
            else:
                await _send(phone, HELP_MESSAGE)
                await _safe_set_state(phone, "idle", {}, db)
            return

        # ── Request for fresh ideas ───────────────────────────────────────────
        _MORE_WORDS = {"more ideas", "give more", "new ideas", "different ideas", "more",
                       "refresh", "new ones", "other ideas", "give me more", "more options"}
        if any(w in text for w in _MORE_WORDS):
            await WhatsAppFlowService._send_ideas(phone, user_id, ctx.get("topic", ""), db)
            return

        # ── Ordinals — check longer phrases first ─────────────────────────────
        idx = None
        for phrase, i in sorted(_ORDINALS.items(), key=lambda kv: -len(kv[0])):
            if phrase in text:
                idx = i
                break

        # Substring match against the actual idea text
        if idx is None:
            for i, idea in enumerate(ideas):
                if len(raw_body) > 5 and raw_body.lower() in idea.lower():
                    idx = i
                    break

        if idx is not None and idx < len(ideas):
            await WhatsAppFlowService._create_and_show_content(phone, ideas[idx], user_id, {}, db)
            return

        # ── AI fallback ───────────────────────────────────────────────────────
        if ideas:
            intent = await _ai_intent(
                raw_body,
                ["first", "second", "third", "none", "more_ideas", "unknown"],
                f"The user was shown 3 ideas: 1) {ideas[0]} 2) {ideas[1] if len(ideas) > 1 else ''} 3) {ideas[2] if len(ideas) > 2 else ''}. "
                "They may pick one, ask for different ideas, or say none/back.",
            )
            pick = {"first": 0, "second": 1, "third": 2}.get(intent)
            if pick is not None and pick < len(ideas):
                await WhatsAppFlowService._create_and_show_content(phone, ideas[pick], user_id, {}, db)
                return
            if intent == "none":
                if ctx.get("headline"):
                    await _send(phone, _format_content(ctx))
                    await _safe_set_state(phone, "showing_content", ctx, db)
                else:
                    await _send(phone, HELP_MESSAGE)
                    await _safe_set_state(phone, "idle", {}, db)
                return
            if intent == "more_ideas":
                await WhatsAppFlowService._send_ideas(phone, user_id, ctx.get("topic", ""), db)
                return

        # ── Gentle re-prompt (last resort) ────────────────────────────────────
        lines = "\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(ideas))
        await _send(
            phone,
            f"Which idea would you like to use?\n\n{lines}\n\n"
            "Say *first*, *second*, or *third* — or *more ideas* for new ones, *back* to return."
        )

    # ── Edit flow ─────────────────────────────────────────────────────────────

    @staticmethod
    async def _handle_edit_choice(
        phone: str,
        text: str,
        raw_body: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        if text in _BACK_WORDS:
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        # Detect "change X to Y" or "set X to Y" patterns — apply directly
        inline = re.match(
            r"(?:change|set|update|rewrite|make)\s+(?:the\s+)?(headline|subheadline|sub.headline|tone|caption)\s+(?:to|as)\s+(.+)",
            raw_body, re.IGNORECASE,
        )
        if inline:
            field_raw = inline.group(1).lower().replace("-", "")
            field = "subheadline" if "sub" in field_raw else field_raw
            value = inline.group(2).strip()
            if field == "tone":
                new_ctx = {**ctx, "tone": value}
                await WhatsAppFlowService._create_and_show_content(phone, ctx.get("topic", value), user_id, new_ctx, db)
            else:
                new_ctx = {**ctx, field: value}
                await _send(phone, _format_content(new_ctx))
                await _safe_set_state(phone, "showing_content", new_ctx, db)
            return

        # Detect tone requests: "make it funnier", "more professional", "write it boldly"
        tone_match = re.match(
            r"(?:make\s+it\s+|more\s+|write\s+(?:it\s+)?|be\s+more\s+|sound\s+more\s+)(.+)",
            raw_body, re.IGNORECASE,
        )
        tone_words = {"funny", "funnier", "professional", "bold", "bolder", "casual", "friendly",
                      "inspirational", "motivational", "witty", "serious", "playful", "confident",
                      "authoritative", "conversational", "educational", "exciting", "energetic"}
        if tone_match:
            candidate = tone_match.group(1).strip().rstrip(".")
            if any(w in candidate.lower() for w in tone_words):
                new_ctx = {**ctx, "tone": candidate}
                await WhatsAppFlowService._create_and_show_content(phone, ctx.get("topic", ""), user_id, new_ctx, db)
                return

        # Detect field from text
        field = None
        if "headline" in text and "sub" not in text:
            field = "headline"
        elif "subheadline" in text or "sub headline" in text or "subhead" in text or "sub" in text:
            field = "subheadline"
        elif "tone" in text or "voice" in text or "style" in text or "vibe" in text:
            field = "tone"
        elif "caption" in text or "body" in text or "text" in text:
            field = "caption"

        if field:
            prompts = {
                "headline": "What should the new headline be?",
                "subheadline": "What should the new subheadline be?",
                "tone": "What tone would you like? (e.g. motivational, funny, bold, professional)",
                "caption": "Type the new caption:",
            }
            await _send(phone, prompts[field])
            await _safe_set_state(phone, "awaiting_edit_value", {**ctx, "edit_field": field}, db)
            return

        # Ask what to edit if we still don't know
        await _send(
            phone,
            "What would you like to change?\n\n"
            "Say *headline*, *subheadline*, *tone*, or describe what you want — "
            "e.g. *make it funnier* or *change the headline to something bolder*"
        )
        await _safe_set_state(phone, "awaiting_edit_choice", ctx, db)

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
            await _send(phone, _format_content(new_ctx))
            await _safe_set_state(phone, "showing_content", new_ctx, db)

    # ── Graphic generation ────────────────────────────────────────────────────

    @staticmethod
    async def _generate_graphic(
        phone: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        # Credit check — 1 credit per graphic generation
        allowed = await _check_and_deduct_credit(user_id, reason="whatsapp_graphic_generation")
        if not allowed:
            await _send(
                phone,
                "⚠️ You've run out of credits.\n\n"
                "Upgrade your plan on the Uri Social dashboard to generate graphics."
            )
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        await _send(phone, "Creating your design... 🎨")
        # Lock state so any messages during the 2-3 min generation get a friendly bounce
        await _safe_set_state(phone, "generating_graphic", ctx, db)

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
            await _send(phone, "Could not generate the graphic right now. Please try again.")
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        if not image_result.get("status"):
            await _send(phone, "Graphic generation failed. Please try again.")
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
                await _send(phone, "Your design is ready 👆", media_url=public_url)
            except Exception as e:
                print(f"[WhatsApp] Twilio media send failed: {e}")
                await _send(phone, f"Your design is ready 👆\n\n🔗 {public_url}")
            await _send(phone, GRAPHIC_ACTIONS)
        else:
            await _send(phone, "Your design is ready 👆\n\nPreview unavailable. Check your Uri Social dashboard.")
            await _send(phone, GRAPHIC_ACTIONS)

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
        if text in _BACK_WORDS:
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
            return

        if any(w in text for w in _POST_NOW_WORDS) or text == "1":
            await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
            return

        if any(w in text for w in _SCHEDULE_WORDS) or text == "2":
            await _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
            return

        if any(w in text for w in ("download", "link", "url", "get link")) or text == "3":
            url = ctx.get("last_graphic_url")
            if url:
                await _send(phone, f"🔗 Download your graphic:\n{url}")
            else:
                await _send(phone, "Link unavailable. Generate a new graphic below.")
            await _send(phone, GRAPHIC_ACTIONS)
            return

        if any(w in text for w in _EDIT_WORDS) or text == "4":
            await WhatsAppFlowService._handle_edit_choice(phone, text, text, user_id, ctx, db)
            return

        if any(w in text for w in ("regenerate", "new design", "try again", "redo", "another design", "different design")) or text == "5":
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
            return

        # AI fallback
        intent = await _ai_intent(
            text,
            ["post_now", "schedule", "download", "edit", "regenerate", "back", "unknown"],
            "The user has just seen a generated graphic for their social media post.",
        )

        if intent == "post_now":
            await WhatsAppFlowService._initiate_post(phone, user_id, ctx, "now", db)
        elif intent == "schedule":
            await _send(phone, SCHEDULE_PROMPT)
            await _safe_set_state(phone, "awaiting_schedule_time", ctx, db)
        elif intent == "download":
            url = ctx.get("last_graphic_url")
            await _send(phone, f"🔗 Download your graphic:\n{url}" if url else "Link unavailable.")
            await _send(phone, GRAPHIC_ACTIONS)
        elif intent == "edit":
            await WhatsAppFlowService._handle_edit_choice(phone, text, text, user_id, ctx, db)
        elif intent == "regenerate":
            await WhatsAppFlowService._generate_graphic(phone, user_id, ctx, db)
        elif intent == "back":
            await _send(phone, _format_content(ctx))
            await _safe_set_state(phone, "showing_content", ctx, db)
        else:
            await _send(phone, GRAPHIC_ACTIONS)

    # ── Re-engagement handler ─────────────────────────────────────────────────

    @staticmethod
    async def _handle_re_engagement(
        phone: str,
        text: str,
        user_id: str,
        ctx: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        yes_words = {"yes", "yeah", "sure", "ok", "okay", "yep", "yup", "go ahead", "show me", "let's go", "absolutely", "definitely"}
        no_words = {"no", "nope", "later", "not now", "maybe later", "busy", "nah"}

        if any(w in text for w in yes_words):
            brand = await _brand_context(user_id, db)
            industry = (brand or {}).get("industry", "your niche")
            await WhatsAppFlowService._create_and_show_content(
                phone, f"a powerful truth about {industry}", user_id, {}, db
            )
        elif any(w in text for w in no_words):
            await _send(phone, "No problem! Just message me anytime you're ready to create content.")
            await _safe_set_state(phone, "idle", {}, db)
        else:
            await _send(phone, "Want to see your content ideas? Just say *yes* or *later*.")

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
                        await _send(phone, RE_ENGAGEMENT)
                        await _safe_set_state(
                            phone, "awaiting_re_engagement", session.get("context", {}), db
                        )
                        sent += 1
                        continue

                brand = await _brand_context(user_id, db)
                if not brand:
                    continue

                # Check and deduct 1 credit before generating daily content
                allowed = await _check_and_deduct_credit(user_id, reason="whatsapp_content_generation")
                if not allowed:
                    # User is out of credits — notify them instead of generating
                    await _send(
                        phone,
                        f"Good morning {first_name} 👋\n\n"
                        "⚠️ You've run out of credits and can't receive today's content.\n\n"
                        "Upgrade your plan at urisocial.com to keep getting daily content."
                    )
                    failed += 1
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

                await _send(phone, f"Good morning {first_name} 👋\n\nYour content for today is ready.")
                await _send(phone, _format_content(new_ctx))
                await _safe_set_state(phone, "showing_content", new_ctx, db)
                sent += 1

            except Exception as e:
                print(f"Daily push failed for {phone}: {e}")
                failed += 1

        return {"sent": sent, "failed": failed, "total": len(users)}
