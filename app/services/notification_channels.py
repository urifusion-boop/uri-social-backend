"""
Delivery channels for Brand Playbook notifications.

One small async sender per channel. Each returns (sent, error) and never
raises — a channel that isn't configured, or a recipient who hasn't
connected it yet, is reported as not sent with a plain reason, so one
broken channel never stops the others.

Channels and what each needs:
- email     SMTP (existing EmailService) + the user's email.
- whatsapp  Twilio WhatsApp (existing) + the user's linked WhatsApp number.
            WhatsApp only allows free-form business messages within 24h of
            the user's last message; outside that, Meta requires an approved
            template — set TWILIO_WHATSAPP_NOTIFY_TEMPLATE_SID to one with
            {{1}} = title and {{2}} = message.
- sms       Twilio SMS (TWILIO_SMS_FROM) + the user's phone number.
- slack     The brand's Slack incoming-webhook URL (Playbook).
- telegram  TELEGRAM_BOT_TOKEN + the brand's chat, linked via the bot.
- push      Web Push (VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY) + a browser
            subscription saved when the user enabled push.
"""
from __future__ import annotations

import asyncio
import html
import json
from typing import Optional, Tuple

import httpx

from app.core.config import settings

Result = Tuple[bool, Optional[str]]


def _text(title: str, message: str, link: Optional[str]) -> str:
    parts = [title, message]
    if link:
        parts.append(link)
    return "\n\n".join(p for p in parts if p)


async def send_email(to_email: str, title: str, message: str, link: Optional[str]) -> Result:
    if not to_email:
        return False, "No email address on the account"
    from app.services.EmailService import email_service

    button = (
        f'<p><a href="{html.escape(link)}" style="background:#CD1B78;color:#fff;padding:10px 18px;'
        f'border-radius:8px;text-decoration:none;font-weight:600">Open URI Social</a></p>'
        if link else ""
    )
    body = (
        f"<div style=\"font-family:Arial,sans-serif;font-size:15px;color:#111;line-height:1.5\">"
        f"<h2 style=\"font-size:18px\">{html.escape(title)}</h2>"
        f"<p>{html.escape(message).replace(chr(10), '<br>')}</p>{button}</div>"
    )
    ok = await email_service.send_raw_email(to_email=to_email, subject=title, html_body=body)
    return (True, None) if ok else (False, "Email delivery failed (is SMTP configured?)")


def _twilio_client():
    from twilio.rest import Client  # type: ignore

    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


async def send_whatsapp(phone: str, title: str, message: str, link: Optional[str]) -> Result:
    if not phone:
        return False, "No WhatsApp number linked — link WhatsApp in Settings"
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_WHATSAPP_FROM):
        return False, "WhatsApp sending isn't configured"
    to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
    kwargs = {"from_": settings.TWILIO_WHATSAPP_FROM, "to": to}
    template_sid = getattr(settings, "TWILIO_WHATSAPP_NOTIFY_TEMPLATE_SID", "")
    if template_sid:
        kwargs["content_sid"] = template_sid
        kwargs["content_variables"] = json.dumps({"1": title, "2": message[:900]})
    else:
        kwargs["body"] = _text(title, message, link)[:1500]
    try:
        await asyncio.to_thread(_twilio_client().messages.create, **kwargs)
        return True, None
    except Exception as exc:
        return False, f"WhatsApp send failed: {exc}"


async def send_sms(phone: str, title: str, message: str, link: Optional[str]) -> Result:
    if not phone:
        return False, "No phone number on the account"
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and getattr(settings, "TWILIO_SMS_FROM", "")):
        return False, "SMS sending isn't configured"
    number = phone.replace("whatsapp:", "")
    try:
        await asyncio.to_thread(
            _twilio_client().messages.create,
            from_=settings.TWILIO_SMS_FROM, to=number, body=_text(title, message, link)[:480],
        )
        return True, None
    except Exception as exc:
        return False, f"SMS send failed: {exc}"


def is_slack_webhook(url: str) -> bool:
    return isinstance(url, str) and url.startswith("https://hooks.slack.com/")


async def send_slack(webhook_url: str, title: str, message: str, link: Optional[str]) -> Result:
    if not is_slack_webhook(webhook_url):
        return False, "No Slack webhook connected — add one in Brand Playbook → Notifications"
    text = f"*{title}*\n{message}" + (f"\n<{link}|Open URI Social>" if link else "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json={"text": text})
        return (True, None) if r.status_code < 300 else (False, f"Slack returned {r.status_code}")
    except Exception as exc:
        return False, f"Slack send failed: {exc}"


async def send_telegram(chat_id: str, title: str, message: str, link: Optional[str]) -> Result:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False, "Telegram isn't configured"
    if not chat_id:
        return False, "Telegram not connected — connect it in Brand Playbook → Notifications"
    text = f"<b>{html.escape(title)}</b>\n{html.escape(message)}" + (f"\n{html.escape(link)}" if link else "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        return (True, None) if r.status_code < 300 else (False, f"Telegram returned {r.status_code}")
    except Exception as exc:
        return False, f"Telegram send failed: {exc}"


async def send_push(subscriptions: list, title: str, message: str, link: Optional[str]) -> Tuple[Result, list]:
    """Web Push to every browser the user enabled. Returns the result and the
    endpoints the push service says are gone (to delete)."""
    private_key = getattr(settings, "VAPID_PRIVATE_KEY", "")
    if not (private_key and getattr(settings, "VAPID_PUBLIC_KEY", "")):
        return (False, "Push notifications aren't configured"), []
    if not subscriptions:
        return (False, "Push isn't enabled on any browser"), []
    try:
        from pywebpush import WebPushException, webpush  # type: ignore
    except ImportError:
        return (False, "pywebpush isn't installed"), []
    payload = json.dumps({"title": title, "body": message[:300], "url": link or "/"})
    claims = {"sub": getattr(settings, "VAPID_SUBJECT", "") or "mailto:noreply@urisocial.com"}
    sent, gone, last_error = 0, [], None
    for sub in subscriptions:
        info = {"endpoint": sub.get("endpoint"), "keys": sub.get("keys") or {}}
        try:
            await asyncio.to_thread(
                webpush, subscription_info=info, data=payload,
                vapid_private_key=private_key, vapid_claims=dict(claims),
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                gone.append(sub.get("endpoint"))
            last_error = f"Push failed: {exc}"
        except Exception as exc:
            last_error = f"Push failed: {exc}"
    return ((True, None) if sent else (False, last_error or "Push failed")), gone
