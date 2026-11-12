"""
fal.ai account-level failures — the fal account running out of balance.

Like the OpenAI account running out of credit (AIService._alert_openai_out_of_credit), this is not
any one user's problem: until someone tops up fal, every video (and every fal-hosted image model)
fails. fal has no balance API, so the only signal is the error itself, the moment a real call is
refused. fal answers a locked account with HTTP 403 and
    {"detail": "User is locked. Reason: Exhausted balance. Top up your balance at fal.ai/dashboard/billing."}
fal_client puts that `detail` in the exception's text.

The customer is told something calm and true ("temporarily unavailable, credits returned") —
never that our fal account is empty — and the admin is told what actually happened.
"""
from typing import Any

# Matched against the lower-cased error text. Specific on purpose: a plain "403" or "billing"
# would also catch a bad API key or an unrelated refusal.
_OUT_OF_CREDIT_HINTS = (
    "exhausted balance",
    "user is locked",
    "top up your balance",
    "insufficient balance",
    "insufficient credit",
    "fal.ai/dashboard/billing",
)

# Set on a video job / clip so the page can show a notice instead of a raw failure.
ERROR_CODE = "video_service_unavailable"

# Shown for the whole job.
JOB_MESSAGE = (
    "Video generation is temporarily unavailable on our side. It's nothing you did, and any credits "
    "for scenes that weren't made are returned automatically. We've been notified; please try again "
    "in a little while, and contact support if it keeps happening."
)
# Shown on each scene that wasn't made — short, since the job message above carries the detail.
CLIP_MESSAGE = "Not made — video generation is temporarily unavailable. Any credits for this scene are returned."


def _text_of(error: Any) -> str:
    text = str(error)
    # The HTTP response body, when the exception carries one (an httpx / fal_client HTTP error).
    response = getattr(error, "response", None)
    body = getattr(response, "text", None)
    if isinstance(body, str):
        text += " " + body
    return text.lower()


def is_out_of_credit(error: Any) -> bool:
    """True when `error` (an exception or its text) is fal refusing a call because our balance is used up."""
    text = _text_of(error)
    return any(hint in text for hint in _OUT_OF_CREDIT_HINTS)


async def alert_out_of_credit(error: Any) -> None:
    """Tell the admin (dashboard alert + email, rate-limited by platform_alerts) that fal is out of credit.
    Never allowed to break the real error path if the DB or email is unreachable."""
    try:
        from app.database import get_db
        from app.services import platform_alerts

        await platform_alerts.raise_alert(
            get_db(),
            kind="fal_insufficient_balance",
            title="fal.ai account is out of credit",
            detail=(
                "Video generation, and any image model hosted on fal, is down until the balance is topped up "
                "at fal.ai/dashboard/billing (or auto-recharge is turned on). "
                f"fal says: “{str(error)[:300]}”"
            ),
        )
    except Exception as e:
        print(f"[fal] failed to raise the out-of-credit platform alert: {e}")
