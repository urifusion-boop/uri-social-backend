"""Six-digit email codes (verification, password reset) that can't be brute-forced.

They used to come from random.randint, be compared with !=, and accept unlimited
guesses for 15 minutes — a script could walk the whole 000000-999999 space against
/reset-password for any known email. Now: a CSPRNG code, a constant-time compare, and
the code is destroyed after MAX_ATTEMPTS wrong guesses.
"""
import hmac
import secrets
from datetime import datetime

from fastapi import HTTPException
from pymongo import ReturnDocument

MAX_ATTEMPTS = 5


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


async def check_one_time_code(db, user: dict, provided: str, code_field: str,
                              expires_field: str, attempts_field: str, label: str) -> None:
    """Raise HTTPException(400) unless `provided` is the user's live code."""
    stored = str(user.get(code_field) or "")
    if not stored:
        raise HTTPException(status_code=400, detail=f"Invalid {label} code. Please request a new one.")
    expires = user.get(expires_field)
    if isinstance(expires, datetime) and expires < datetime.utcnow():
        raise HTTPException(status_code=400, detail=f"This {label} code has expired. Please request a new one.")
    if hmac.compare_digest(stored.encode(), str(provided or "").strip().encode()):
        return
    updated = await db["users"].find_one_and_update(
        {"_id": user["_id"]}, {"$inc": {attempts_field: 1}}, return_document=ReturnDocument.AFTER)
    if (updated or {}).get(attempts_field, 0) >= MAX_ATTEMPTS:
        await db["users"].update_one(
            {"_id": user["_id"]}, {"$unset": {code_field: "", expires_field: ""}})
        raise HTTPException(status_code=400,
                            detail=f"Too many wrong attempts. Please request a new {label} code.")
    raise HTTPException(status_code=400, detail=f"Invalid {label} code.")
