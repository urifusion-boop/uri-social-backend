"""
Public (no-login) forms on the marketing site: the Contact page's "Send message"
and the API Docs page's "Join the API waitlist".

Both store what they receive (so nothing is lost if SMTP is down or unset) and
email the team inbox so a human sees it.
"""
import time
from collections import defaultdict, deque
from datetime import datetime
from html import escape
from typing import Deque, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr, Field

from app.dependencies import get_db_dependency
from app.services.EmailService import email_service

router = APIRouter(prefix="/public", tags=["Public forms"])

# hello@urisocial.com bounces ("550 5.1.1 User does not exist"); this is the inbox the team reads.
INBOX = "urisocialinsight@gmail.com"

# Must match the <select> on the Contact page.
CONTACT_SUBJECTS = {
    "General Inquiry",
    "Sales / Enterprise",
    "Support",
    "Partnership",
    "Press",
    "Other",
}

# Best-effort per-IP throttle: these endpoints are open to the internet and each
# submission sends an email. Per-process, so it resets on restart and is not shared
# across workers — it stops a casual script, not a determined one.
_RATE_LIMIT = 5
_RATE_WINDOW_SECONDS = 3600
_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttle(request: Request, bucket: str) -> None:
    key = f"{bucket}:{_client_ip(request)}"
    now = time.monotonic()
    hits = _hits[key]
    while hits and now - hits[0] > _RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many submissions. Please try again in a bit.")
    hits.append(now)


def _email_html(title: str, rows: Dict[str, str], body: Optional[str] = None) -> str:
    """Plain internal notification; every user-supplied value is escaped."""
    table = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#6B7280"><b>{escape(k)}</b></td>'
        f"<td>{escape(v)}</td></tr>"
        for k, v in rows.items()
    )
    message = (
        f'<p style="white-space:pre-wrap;border-left:3px solid #CD1B78;padding-left:12px">{escape(body)}</p>'
        if body
        else ""
    )
    return (
        '<div style="font-family:Segoe UI,Roboto,sans-serif;font-size:14px;color:#111827">'
        f"<h3>{escape(title)}</h3><table>{table}</table>{message}</div>"
    )


class ContactRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    subject: str = Field("General Inquiry", max_length=50)
    message: str = Field(..., min_length=2, max_length=5000)
    # Honeypot: hidden from people, filled in by bots.
    website: Optional[str] = Field(None, max_length=200)


class WaitlistRequest(BaseModel):
    email: EmailStr
    name: Optional[str] = Field(None, max_length=100)
    website: Optional[str] = Field(None, max_length=200)


@router.post("/contact")
async def submit_contact(
    body: ContactRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    if body.subject not in CONTACT_SUBJECTS:
        raise HTTPException(status_code=422, detail="Please pick one of the listed subjects.")

    # Pretend success to bots without storing or emailing anything.
    if body.website:
        return {"status": True, "responseCode": 200, "responseMessage": "Message sent."}

    _throttle(request, "contact")

    name = body.name.strip()
    message = body.message.strip()
    await db["contact_messages"].insert_one(
        {
            "name": name,
            "email": str(body.email).lower(),
            "subject": body.subject,
            "message": message,
            "ip": _client_ip(request),
            "status": "new",
            "created_at": datetime.utcnow(),
        }
    )

    background.add_task(
        email_service.send_raw_email,
        to_email=INBOX,
        subject=f"[Contact] {body.subject} — {name}"[:150].replace("\r", " ").replace("\n", " "),
        html_body=_email_html(
            "New message from the Contact page",
            {"Name": name, "Email": str(body.email), "Subject": body.subject},
            message,
        ),
        reply_to=str(body.email),
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Message sent. We typically reply within 4 hours during business hours (WAT).",
    }


@router.post("/api-waitlist")
async def join_api_waitlist(
    body: WaitlistRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    if body.website:
        return {"status": True, "responseCode": 200, "responseMessage": "You're on the list."}

    _throttle(request, "waitlist")

    email = str(body.email).lower()
    name = (body.name or "").strip() or None
    # Upsert so a repeat signup is a quiet success, not a duplicate row or an error
    # that tells a stranger whether an address is already on the list.
    result = await db["api_waitlist"].update_one(
        {"email": email},
        {
            "$setOnInsert": {"email": email, "created_at": datetime.utcnow(), "ip": _client_ip(request)},
            **({"$set": {"name": name}} if name else {}),
        },
        upsert=True,
    )

    if result.upserted_id is not None:
        background.add_task(
            email_service.send_raw_email,
            to_email=INBOX,
            subject="[API waitlist] New signup",
            html_body=_email_html("New API waitlist signup", {"Email": email}),
            reply_to=email,
        )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "You're on the list. We'll email you when API access opens.",
    }
