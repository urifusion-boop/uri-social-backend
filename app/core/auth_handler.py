# app/core/auth_handler.py

import time
import jwt
from typing import Dict, Any
from fastapi import HTTPException, Header
from app.core.config import settings

SECRET_KEY = settings.AUTHJWT_SECRET_KEY
ALGORITHM = "HS256"


def decode_jwt(token: str) -> Any:
    try:
        decoded_token = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if decoded_token["exp"] < time.time():
            raise HTTPException(status_code=403, detail="Token expired.")
        return decoded_token
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=403, detail="Token expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="Invalid token.")
    except Exception as e:
        raise HTTPException(status_code=403, detail="Token validation error: " + str(e))
