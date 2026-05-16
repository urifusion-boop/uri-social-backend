"""
Cron Jobs for API Key Rate Limit Resets

Enterprise-grade rate limit reset jobs to run hourly and daily.
"""

import asyncio
import logging
from datetime import datetime

from app.middleware.api_key_auth import api_key_auth_service

logger = logging.getLogger(__name__)


async def reset_hourly_limits():
    """
    Reset hourly rate limits for all API keys
    Should be run every hour via cron
    """
    try:
        logger.info("Starting hourly rate limit reset...")
        await api_key_auth_service.reset_hourly_limits()
        logger.info("✅ Hourly rate limits reset successfully")
    except Exception as e:
        logger.error(f"❌ Failed to reset hourly limits: {e}")
        raise


async def reset_daily_limits():
    """
    Reset daily rate limits for all API keys
    Should be run once daily via cron
    """
    try:
        logger.info("Starting daily rate limit reset...")
        await api_key_auth_service.reset_daily_limits()
        logger.info("✅ Daily rate limits reset successfully")
    except Exception as e:
        logger.error(f"❌ Failed to reset daily limits: {e}")
        raise


# FastAPI endpoint for cron jobs
from fastapi import APIRouter, Header, HTTPException
from app.core.config import settings

cron_router = APIRouter(prefix="/cron", tags=["Cron Jobs"])


@cron_router.post("/reset-hourly-limits")
async def cron_reset_hourly_limits(
    x_cron_secret: str = Header(None, alias="X-Cron-Secret")
):
    """
    Cron endpoint to reset hourly rate limits

    **Authentication**: Requires X-Cron-Secret header

    **Schedule**: Every hour (0 * * * *)

    **Curl Example**:
    ```bash
    curl -X POST https://api.urisocial.com/cron/reset-hourly-limits \\
      -H "X-Cron-Secret: your-cron-secret"
    ```
    """
    # Verify cron secret
    expected_secret = getattr(settings, "CRON_SECRET", None) or "your-cron-secret-here"
    if not x_cron_secret or x_cron_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid cron secret")

    await reset_hourly_limits()

    return {
        "success": True,
        "message": "Hourly rate limits reset successfully",
        "timestamp": datetime.utcnow().isoformat()
    }


@cron_router.post("/reset-daily-limits")
async def cron_reset_daily_limits(
    x_cron_secret: str = Header(None, alias="X-Cron-Secret")
):
    """
    Cron endpoint to reset daily rate limits

    **Authentication**: Requires X-Cron-Secret header

    **Schedule**: Daily at midnight (0 0 * * *)

    **Curl Example**:
    ```bash
    curl -X POST https://api.urisocial.com/cron/reset-daily-limits \\
      -H "X-Cron-Secret: your-cron-secret"
    ```
    """
    # Verify cron secret
    expected_secret = getattr(settings, "CRON_SECRET", None) or "your-cron-secret-here"
    if not x_cron_secret or x_cron_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid cron secret")

    await reset_daily_limits()

    return {
        "success": True,
        "message": "Daily rate limits reset successfully",
        "timestamp": datetime.utcnow().isoformat()
    }


# Standalone script for manual execution
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python reset_api_key_limits.py [hourly|daily]")
        sys.exit(1)

    mode = sys.argv[1]

    if mode == "hourly":
        asyncio.run(reset_hourly_limits())
    elif mode == "daily":
        asyncio.run(reset_daily_limits())
    else:
        print(f"Invalid mode: {mode}. Use 'hourly' or 'daily'")
        sys.exit(1)


# ============================================
# CRONTAB SETUP INSTRUCTIONS
# ============================================

"""
Add these lines to your crontab (crontab -e):

# Reset API key hourly limits every hour
0 * * * * curl -X POST https://api.urisocial.com/cron/reset-hourly-limits -H "X-Cron-Secret: YOUR_SECRET_HERE" >> /var/log/cron-hourly.log 2>&1

# Reset API key daily limits at midnight
0 0 * * * curl -X POST https://api.urisocial.com/cron/reset-daily-limits -H "X-Cron-Secret: YOUR_SECRET_HERE" >> /var/log/cron-daily.log 2>&1

Or use this script directly:

# Reset hourly limits every hour
0 * * * * cd /path/to/uri-social-backend && python app/cron/reset_api_key_limits.py hourly >> /var/log/cron-hourly.log 2>&1

# Reset daily limits at midnight
0 0 * * * cd /path/to/uri-social-backend && python app/cron/reset_api_key_limits.py daily >> /var/log/cron-daily.log 2>&1
"""
