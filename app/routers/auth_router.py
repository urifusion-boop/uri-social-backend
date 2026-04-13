import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from bson import ObjectId
import secrets

from app.core.auth_handler import sign_jwt
from app.core.config import settings
from app.database import get_db
from app.dependencies import get_db_dependency
from app.domain.responses.uri_response import UriResponse
from app.services.TrialService import trial_service
from app.services.NotificationService import notification_service

router = APIRouter(prefix="/auth", tags=["Auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    first_name: str = ""
    last_name: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    accessToken: str
    userId: str
    email: str
    firstName: str
    lastName: str


@router.post("/signup")
async def signup(body: SignupRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    existing = await db["users"].find_one({"email": body.email})
    if existing:
        raise HTTPException(status_code=409, detail="A user with this email already exists.")

    hashed = pwd_context.hash(body.password)
    user_id = str(uuid.uuid4())
    referral_code = uuid.uuid4().hex[:8].upper()
    result = await db["users"].insert_one({
        "userId": user_id,
        "email": body.email,
        "password": hashed,
        "first_name": body.first_name,
        "last_name": body.last_name,
        "referralCode": referral_code,
    })

    # PRD 5.1: Activate free trial on successful signup
    trial_status = None
    try:
        trial_result = await trial_service.activate_trial(user_id)
        trial_status = {
            "is_trial": trial_result.is_trial,
            "trial_active": trial_result.trial_active,
            "trial_credits": trial_result.trial_credits,
            "credits_remaining": trial_result.credits_remaining,
            "days_remaining": trial_result.days_remaining,
            "trial_end_date": trial_result.trial_end_date.isoformat() if trial_result.trial_end_date else None,
        }
    except Exception as e:
        print(f"⚠️ Trial activation failed for {user_id}: {e}")

    # Notification PRD 4.1: Welcome email on signup
    try:
        import asyncio
        asyncio.ensure_future(notification_service.notify_signup(
            user_id=user_id,
            email=body.email,
            first_name=body.first_name,
            trial_days=trial_status.get("days_remaining", 3) if trial_status else 0,
            trial_credits=trial_status.get("trial_credits", 10) if trial_status else 0,
        ))
    except Exception as e:
        print(f"⚠️ Signup notification failed for {user_id}: {e}")

    token = sign_jwt(user_id, body.email, body.first_name, body.last_name)

    return {
        "status": True,
        "responseCode": 201,
        "responseMessage": "Account created successfully.",
        "responseData": {
            "accessToken": token,
            "userId": user_id,
            "email": body.email,
            "firstName": body.first_name,
            "lastName": body.last_name,
            "trial": trial_status,
        },
    }


class GoogleAuthRequest(BaseModel):
    code: str
    redirect_uri: str


@router.post("/google")
async def google_auth(body: GoogleAuthRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Exchange a Google OAuth authorization code for a URI JWT."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=501, detail="Google OAuth is not configured on this server.")

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Exchange code for tokens
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": body.code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": body.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_data = token_resp.json()
        if "error" in token_data:
            error_code = token_data.get("error", "")
            if error_code == "invalid_grant":
                raise HTTPException(status_code=400, detail="This Google sign-in link has already been used or expired. Please try signing in again.")
            raise HTTPException(status_code=400, detail=f"Google token exchange failed: {token_data.get('error_description', error_code)}")

        access_token = token_data["access_token"]

        # 2. Fetch user info from Google
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo = userinfo_resp.json()

    email = userinfo.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Could not retrieve email from Google account.")

    first_name = userinfo.get("given_name", "")
    last_name = userinfo.get("family_name", "")

    # 3. Find or create user
    existing = await db["users"].find_one({"email": email})
    trial_status = None
    if existing:
        user_id = existing.get("userId") or str(existing["_id"])
        first_name = existing.get("first_name", first_name)
        last_name = existing.get("last_name", last_name)
    else:
        user_id = str(uuid.uuid4())
        referral_code = uuid.uuid4().hex[:8].upper()
        await db["users"].insert_one({
            "userId": user_id,
            "email": email,
            "password": None,  # Google users have no password
            "first_name": first_name,
            "last_name": last_name,
            "referralCode": referral_code,
            "auth_provider": "google",
        })

        # PRD 5.1: Activate free trial on signup
        try:
            trial_result = await trial_service.activate_trial(user_id)
            trial_status = {
                "is_trial": trial_result.is_trial,
                "trial_active": trial_result.trial_active,
                "trial_credits": trial_result.trial_credits,
                "credits_remaining": trial_result.credits_remaining,
                "days_remaining": trial_result.days_remaining,
                "trial_end_date": trial_result.trial_end_date.isoformat() if trial_result.trial_end_date else None,
            }
        except Exception as e:
            print(f"⚠️ Trial activation failed for {user_id}: {e}")

        # Notification PRD 4.1: Welcome email on Google signup
        try:
            import asyncio
            asyncio.ensure_future(notification_service.notify_signup(
                user_id=user_id,
                email=email,
                first_name=first_name,
                trial_days=trial_status.get("days_remaining", 3) if trial_status else 0,
                trial_credits=trial_status.get("trial_credits", 10) if trial_status else 0,
            ))
        except Exception as e:
            print(f"⚠️ Signup notification failed for {user_id}: {e}")

    token = sign_jwt(user_id, email, first_name, last_name)

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Google sign-in successful.",
        "responseData": {
            "accessToken": token,
            "userId": user_id,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "trial": trial_status,
        },
    }


@router.post("/login")
async def login(body: LoginRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    user = await db["users"].find_one({"email": body.email})
    if not user or not pwd_context.verify(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user_id = user.get("userId") or str(user["_id"])
    token = sign_jwt(user_id, user["email"], user.get("first_name", ""), user.get("last_name", ""))

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Login successful.",
        "responseData": {
            "accessToken": token,
            "userId": user_id,
            "email": user["email"],
            "firstName": user.get("first_name", ""),
            "lastName": user.get("last_name", ""),
        },
    }
