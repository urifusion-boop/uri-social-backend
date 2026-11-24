"""Shared-secret checks for machine-to-machine endpoints (cron jobs, internal services).

Fail closed: an unset secret rejects every caller. These endpoints used to compare
against getattr(settings, "CRON_SECRET", "") while CRON_SECRET was never declared on
Settings (extra="ignore" drops undeclared env vars), so the secret was always empty and
the check never ran — anyone on the internet could trigger publishing runs and daily
pushes. An empty expected value must never mean "open".
"""
import hmac

from fastapi import HTTPException, Request


def secret_matches(provided: str, expected: str) -> bool:
    """Constant-time compare; False whenever either side is empty."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def require_cron_secret(request: Request) -> None:
    from app.core.config import settings

    if not secret_matches(request.headers.get("X-Cron-Secret", ""), settings.CRON_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden.")


def require_internal_service_secret(request: Request) -> None:
    from app.core.config import settings

    if not secret_matches(request.headers.get("X-Internal-Secret", ""),
                          settings.INTERNAL_SERVICE_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden.")
