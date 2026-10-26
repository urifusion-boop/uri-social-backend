"""
Unified Inbox — WhatsApp, through Meta's WhatsApp Cloud API.

A business connects its number with Meta's Embedded Signup (the "Connect WhatsApp"
popup): it logs in with Facebook, picks or creates its WhatsApp Business account and
confirms the number. We get back a short-lived code plus the WhatsApp Business
Account (WABA) and phone-number ids, exchange the code for a business token,
subscribe this app to the WABA's webhooks, and register the number as an inbox
channel — the same shape as an Instagram or Facebook channel, so the agent, Brand
Voice, auto-replies and escalations all work on it unchanged.

Messages arrive on the same Meta webhook as Instagram and Messenger, under the object
"whatsapp_business_account". A WhatsApp conversation is keyed by the customer's
WhatsApp id (their number), and the phone-number id is what routes it to a workspace.

The 24-hour rule is WhatsApp's too: free-form replies only within 24 hours of the
customer's last message, approved templates after that.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.core.config import settings

from .entities import CHANNEL_ACCOUNTS, MESSAGES, OUTBOX, Platform, now

TIMEOUT = httpx.Timeout(20.0)
OBJECT = "whatsapp_business_account"

# What a non-text message shows in the inbox until media is fetched.
_PLACEHOLDERS = {
    "image": "[Photo]", "video": "[Video]", "audio": "[Voice note]", "document": "[Document]",
    "sticker": "[Sticker]", "location": "[Location]", "contacts": "[Contact card]",
    "reaction": "[Reaction]", "button": "", "interactive": "", "order": "[Order]",
}


def _graph() -> str:
    return f"https://graph.facebook.com/{getattr(settings, 'FACEBOOK_API_VERSION', '') or 'v21.0'}"


def _ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError):
        return None


def _text_of(m: dict) -> str:
    kind = m.get("type") or ""
    if kind == "text":
        return (m.get("text") or {}).get("body") or ""
    if kind in ("image", "video", "document"):
        caption = (m.get(kind) or {}).get("caption") or ""
        return f"{_PLACEHOLDERS[kind]} {caption}".strip()
    if kind == "button":
        return (m.get("button") or {}).get("text") or ""
    if kind == "interactive":
        i = m.get("interactive") or {}
        reply = i.get("button_reply") or i.get("list_reply") or {}
        return reply.get("title") or ""
    if kind == "reaction":
        return f"Reacted {(m.get('reaction') or {}).get('emoji') or ''}".strip()
    if kind == "location":
        loc = m.get("location") or {}
        return f"[Location] {loc.get('name') or ''} {loc.get('address') or ''}".strip()
    return _PLACEHOLDERS.get(kind, f"[{kind or 'message'}]")


def parse(payload: dict) -> list[dict]:
    """Customer messages in a WhatsApp webhook, as the inbox's normalised events."""
    events: list[dict] = []
    if payload.get("object") != OBJECT:
        return events
    for entry in payload.get("entry") or []:
        for ch in entry.get("changes") or []:
            if ch.get("field") != "messages":
                continue
            v = ch.get("value") or {}
            phone_number_id = str((v.get("metadata") or {}).get("phone_number_id") or "")
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") or ""
                     for c in v.get("contacts") or []}
            for m in v.get("messages") or []:
                wa_id = str(m.get("from") or "")
                if not (phone_number_id and wa_id and m.get("id")):
                    continue
                media_kind = m.get("type") if m.get("type") in ("image", "video", "audio", "document", "sticker") else ""
                ref = m.get("referral") or {}
                events.append({
                    "type": "dm",
                    "external_account_id": phone_number_id,
                    "external_user_id": wa_id,
                    "external_thread_id": wa_id,
                    # A WhatsApp id is the customer's number; shown when they have no
                    # profile name, rather than looking one up that doesn't exist.
                    "display_name": names.get(wa_id) or f"+{wa_id}",
                    "provider_message_id": str(m["id"]),
                    "text": _text_of(m),
                    "attachments": [{"type": media_kind, "media_id": (m.get(media_kind) or {}).get("id"),
                                     "mime_type": (m.get(media_kind) or {}).get("mime_type")}]
                    if media_kind else [],
                    "provider_timestamp": _ts(m.get("timestamp")),
                    # A Click-to-WhatsApp ad says which ad opened the chat.
                    "source_ad_id": str(ref.get("source_id") or "") if ref.get("source_type") == "ad" else "",
                })
    return events


def statuses(payload: dict) -> list[dict]:
    """Delivery updates for messages we sent: sent, delivered, read, failed."""
    out: list[dict] = []
    if payload.get("object") != OBJECT:
        return out
    for entry in payload.get("entry") or []:
        for ch in entry.get("changes") or []:
            for s in (ch.get("value") or {}).get("statuses") or []:
                errs = s.get("errors") or []
                out.append({"id": str(s.get("id") or ""), "status": s.get("status") or "",
                            "at": _ts(s.get("timestamp")),
                            "error": (errs[0].get("title") or errs[0].get("message") or "") if errs else ""})
    return out


async def apply_statuses(db, payload: dict) -> int:
    """Record what WhatsApp says happened to our replies. A 'failed' is surfaced on the
    message — WhatsApp accepted it, then could not deliver it (e.g. the 24-hour window)."""
    n = 0
    for s in statuses(payload):
        if not s["id"]:
            continue
        update = {"provider_status": s["status"], "provider_status_at": s["at"] or now()}
        if s["status"] == "failed":
            update["failure_reason"] = s["error"] or "WhatsApp couldn't deliver this message"
        res = await db[MESSAGES].update_one({"provider_message_id": s["id"]}, {"$set": update})
        await db[OUTBOX].update_one({"provider_message_id": s["id"]}, {"$set": update})
        n += res.modified_count
    return n


# ── Connecting a number ──────────────────────────────────────────────────────

class WhatsAppConnectError(Exception):
    pass


async def _call(client: httpx.AsyncClient, method: str, path: str, token: str, **kw) -> dict:
    params = {**(kw.pop("params", None) or {}), "access_token": token}
    r = await client.request(method, f"{_graph()}/{path.lstrip('/')}", params=params, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"message": r.text[:200]}}
    if "error" in body:
        err = body["error"]
        raise WhatsAppConnectError(err.get("error_user_msg") or err.get("message") or "Meta refused the request")
    return body


async def exchange_code(code: str) -> str:
    """The Embedded Signup code → a business integration token for this client."""
    if not (settings.META_APP_ID and settings.META_APP_SECRET):
        raise WhatsAppConnectError("WhatsApp connection isn't configured (META_APP_ID / META_APP_SECRET).")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(f"{_graph()}/oauth/access_token", params={
            "client_id": settings.META_APP_ID, "client_secret": settings.META_APP_SECRET, "code": code})
    body = r.json()
    if not body.get("access_token"):
        err = body.get("error") or {}
        raise WhatsAppConnectError(err.get("message") or "Meta didn't return a token for that sign-in.")
    return body["access_token"]


async def connect(db, workspace_id: str, *, token: str, waba_id: str, phone_number_id: str,
                  coexistence: bool = False, actor: str = "") -> dict:
    """Make this number an inbox channel for the workspace. Safe to re-run."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Messages for this WABA come to our webhook only once the app is subscribed.
        await _call(client, "POST", f"{waba_id}/subscribed_apps", token)
        number = await _call(client, "GET", phone_number_id, token, params={
            "fields": "display_phone_number,verified_name,code_verification_status,quality_rating"})
        pin = ""
        # A number moved onto the Cloud API must be registered once; one still used in
        # the WhatsApp Business app alongside us (coexistence) must not be.
        if not coexistence:
            pin = f"{secrets.randbelow(10**6):06d}"
            try:
                await _call(client, "POST", f"{phone_number_id}/register", token,
                            json={"messaging_product": "whatsapp", "pin": pin})
            except WhatsAppConnectError as e:
                # Already registered is fine; anything else is a real failure.
                if "already" not in str(e).lower():
                    raise
                pin = ""

    row = {
        "workspace_id": workspace_id,
        "platform": Platform.WHATSAPP.value,
        "external_account_id": phone_number_id,
        "page_id": "",
        "waba_id": waba_id,
        "access_token": token,
        "name": number.get("verified_name") or number.get("display_phone_number") or "WhatsApp",
        "display_phone_number": number.get("display_phone_number") or "",
        "quality_rating": number.get("quality_rating") or "",
        "coexistence": coexistence,
        "updated_at": now(),
        "connected_by": actor,
    }
    if pin:
        row["registration_pin"] = pin
    await db[CHANNEL_ACCOUNTS].update_one(
        {"workspace_id": workspace_id, "platform": Platform.WHATSAPP.value,
         "external_account_id": phone_number_id},
        {"$set": row, "$setOnInsert": {"connected_at": now()}}, upsert=True)
    from .channels import prune_stale_claims
    await prune_stale_claims(db, workspace_id, phone_number_id)
    return {k: row[k] for k in ("name", "display_phone_number", "external_account_id", "quality_rating")}


# ── Sending ──────────────────────────────────────────────────────────────────

async def _send(account: dict, payload: dict) -> str:
    from .send import ProviderTimeout
    token = account.get("access_token") or ""
    phone_number_id = account.get("external_account_id") or ""
    if not (token and phone_number_id):
        raise Exception("this WhatsApp number isn't fully connected")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{_graph()}/{phone_number_id}/messages",
                                  params={"access_token": token},
                                  json={"messaging_product": "whatsapp", **payload})
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderTimeout(str(e)) from e
    body = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        if err.get("code") == 131047:
            raise Exception("More than 24 hours since the customer's last message — send a template instead")
        raise Exception(f"WhatsApp rejected the message: {err.get('message') or r.status_code}")
    return ((body.get("messages") or [{}])[0]).get("id") or ""


async def send_text(account: dict, to: str, text: str) -> str:
    return await _send(account, {"to": to, "type": "text", "text": {"body": text, "preview_url": True}})


async def send_template(account: dict, to: str, name: str, language: str,
                        params: Optional[list[str]] = None) -> str:
    template: dict[str, Any] = {"name": name, "language": {"code": language}}
    if params:
        template["components"] = [{"type": "body",
                                   "parameters": [{"type": "text", "text": p} for p in params]}]
    return await _send(account, {"to": to, "type": "template", "template": template})


async def list_templates(account: dict) -> list[dict]:
    """The WABA's approved templates — the only thing that may go out after 24 hours."""
    waba_id, token = account.get("waba_id") or "", account.get("access_token") or ""
    if not (waba_id and token):
        return []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        body = await _call(client, "GET", f"{waba_id}/message_templates", token, params={
            "fields": "name,language,status,category,components", "limit": 100})
    out = []
    for t in body.get("data") or []:
        if t.get("status") != "APPROVED":
            continue
        body_c = next((c for c in t.get("components") or [] if c.get("type") == "BODY"), {})
        text = body_c.get("text") or ""
        out.append({"name": t["name"], "language": t.get("language"), "category": t.get("category"),
                    "body": text, "variables": text.count("{{")})
    return out
