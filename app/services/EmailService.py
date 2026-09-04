"""
Email Service
Aligned with Notification System PRD V1 — Section 6

Handles:
- SMTP email delivery (PRD 6.1)
- Retry logic (PRD 10: max 3 attempts)
- Template rendering with Jinja2 (PRD 6.2 / 6.3)
"""
import asyncio
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from app.core.config import settings

MAX_RETRIES = 3  # PRD Section 10: Retry sending (max 3 attempts)

# Template engine setup (PRD 6.2)
TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "email"
_jinja_env: Optional[Environment] = None


def _get_jinja_env() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )
    return _jinja_env


def _make_smtp_client() -> "aiosmtplib.SMTP":
    """
    A fresh SMTP client for one send attempt (both send_email and send_raw_email
    create a new connection per attempt rather than reusing one, to avoid stale
    connections across retries).

    use_tls (implicit TLS from the first byte, correct for port 465) was
    previously hardcoded True regardless of the actual configured port —  but
    SMTP_PORT has no explicit value set in either environment, so it silently
    defaulted to 587 (config.py) instead, which is the STARTTLS port: it
    expects a plaintext EHLO first and only upgrades to TLS after an explicit
    STARTTLS command. Sending a TLS ClientHello where the server expects
    plaintext SMTP is exactly what produces "Unexpected EOF received" —
    confirmed live, every verification email failed on both dev and prod.
    Derive the correct mode from the actual port instead of assuming one.
    """
    implicit_tls = settings.SMTP_PORT == 465
    return aiosmtplib.SMTP(
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        use_tls=implicit_tls,
        start_tls=not implicit_tls,
        timeout=30,
    )


class EmailService:
    """
    Async SMTP email delivery service.
    PRD 6.1: SMTP or API-based provider
    PRD 6.3: Template variables — user name, content preview, CTA links
    """

    def __init__(self):
        self._configured = bool(settings.SMTP_HOST and settings.SMTP_USERNAME)

    @property
    def is_configured(self) -> bool:
        return self._configured

    async def send_email(
        self,
        to_email: str,
        subject: str,
        template_name: str,
        template_vars: dict,
    ) -> bool:
        """
        Send an email using a Jinja2 HTML template.
        PRD 10: Retries up to 3 times on failure.

        Returns True if sent successfully, False otherwise.
        """
        if not self.is_configured:
            print(f"⚠️ Email not configured — skipping send to {to_email}: {subject}")
            return False

        # Render template (PRD 6.2 / 6.3)
        try:
            env = _get_jinja_env()
            template = env.get_template(f"{template_name}.html")
            html_body = template.render(**template_vars)
        except Exception as e:
            print(f"❌ Email template render failed ({template_name}): {e}")
            return False

        # Build message
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        # PRD 10: Retry sending (max 3 attempts)
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # A fresh connection per attempt (avoids stale connections across
                # retries) — see _make_smtp_client's docstring for the TLS/port fix.
                smtp = _make_smtp_client()

                await smtp.connect()
                await smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                await smtp.send_message(msg)
                await smtp.quit()

                return True
            except (aiosmtplib.SMTPException, ConnectionError, TimeoutError, OSError) as e:
                last_error = e
                print(f"⚠️ Email send attempt {attempt}/{MAX_RETRIES} failed: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
            except Exception as e:
                last_error = e
                print(f"⚠️ Email send attempt {attempt}/{MAX_RETRIES} failed (unexpected): {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)

        print(f"❌ Email send failed after {MAX_RETRIES} attempts to {to_email}: {last_error}")
        return False

    async def send_raw_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
    ) -> bool:
        """Send an email with pre-rendered HTML body."""
        if not self.is_configured:
            print(f"⚠️ Email not configured — skipping send to {to_email}")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # A fresh connection per attempt (avoids stale connections across
                # retries) — see _make_smtp_client's docstring for the TLS/port fix.
                smtp = _make_smtp_client()

                await smtp.connect()
                await smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                await smtp.send_message(msg)
                await smtp.quit()

                return True
            except (aiosmtplib.SMTPException, ConnectionError, TimeoutError, OSError) as e:
                last_error = e
                print(f"⚠️ Raw email send attempt {attempt}/{MAX_RETRIES} failed: {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
            except Exception as e:
                last_error = e
                print(f"⚠️ Raw email send attempt {attempt}/{MAX_RETRIES} failed (unexpected): {e}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(2 ** attempt)

        print(f"❌ Raw email send failed after {MAX_RETRIES} attempts: {last_error}")
        return False


# Singleton
email_service = EmailService()
