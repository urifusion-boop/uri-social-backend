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
                # Create a new connection for each attempt to avoid stale connections
                # Port 465 uses direct SSL/TLS connection (no STARTTLS needed)
                smtp = aiosmtplib.SMTP(
                    hostname=settings.SMTP_HOST,
                    port=settings.SMTP_PORT,
                    use_tls=True,  # Direct SSL/TLS for port 465
                    timeout=30,
                )

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
                # Create a new connection for each attempt to avoid stale connections
                # Port 465 uses direct SSL/TLS connection (no STARTTLS needed)
                smtp = aiosmtplib.SMTP(
                    hostname=settings.SMTP_HOST,
                    port=settings.SMTP_PORT,
                    use_tls=True,  # Direct SSL/TLS for port 465
                    timeout=30,
                )

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
