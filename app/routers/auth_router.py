import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, Header
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from bson import ObjectId
import secrets
import random
from datetime import datetime, timedelta

from app.core.auth_handler import sign_jwt
from app.core.config import settings
from app.database import get_db
from app.dependencies import get_db_dependency
from app.domain.responses.uri_response import UriResponse
from app.services.TrialService import trial_service
from app.services.NotificationService import notification_service
from app.services.EmailService import email_service
from app.services import PostHogService

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
    now = datetime.utcnow()

    # Generate 6-digit verification code
    verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    verification_code_expires = now + timedelta(minutes=15)  # Code expires in 15 minutes

    try:
        result = await db["users"].insert_one({
            "userId": user_id,
            "email": body.email,
            "password": hashed,
            "first_name": body.first_name,
            "last_name": body.last_name,
            "referralCode": referral_code,
            # New fields with defaults
            "role": "user",
            "created_at": now,
            "updated_at": now,
            "is_active": False,  # Set to False until email verified
            "email_verified": False,
            "verification_code": verification_code,
            "verification_code_expires": verification_code_expires,
            "account_status": "pending_verification",
            "last_login_at": None,
            "last_seen_at": now,
            "phone": None,
            "timezone": "UTC",
            "language": "en",
        })
    except Exception as e:
        # Duplicate key error (if email index exists) or other DB errors
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            raise HTTPException(status_code=409, detail="A user with this email already exists.")
        else:
            print(f"❌ User creation failed for {body.email}: {e}")
            raise HTTPException(status_code=500, detail="Account creation failed. Please try again.")

    PostHogService.track_signup(user_id, email=body.email, method="email")

    # Send verification email - ONLY send if insert succeeded
    try:
        import asyncio
        asyncio.ensure_future(email_service.send_email(
            to_email=body.email,
            subject="Verify your URI Social account",
            template_name="email_verification",
            template_vars={
                "first_name": body.first_name or "there",
                "verification_code": verification_code,
                "expires_in": "15 minutes",
            }
        ))
    except Exception as e:
        print(f"⚠️ Verification email failed for {user_id}: {e}")

    return {
        "status": True,
        "responseCode": 201,
        "responseMessage": "Account created successfully. Please check your email for verification code.",
        "responseData": {
            "userId": user_id,
            "email": body.email,
            "requiresVerification": True,
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
    is_new_user = not existing
    trial_status = None
    if existing:
        user_id = existing.get("userId") or str(existing["_id"])
        first_name = existing.get("first_name", first_name)
        last_name = existing.get("last_name", last_name)
        # Update last_login_at for existing users
        await db["users"].update_one(
            {"email": email},
            {"$set": {"last_login_at": datetime.utcnow(), "last_seen_at": datetime.utcnow()}}
        )
        PostHogService.track_login(user_id, email=email, method="google")
    else:
        user_id = str(uuid.uuid4())
        referral_code = uuid.uuid4().hex[:8].upper()
        now = datetime.utcnow()

        await db["users"].insert_one({
            "userId": user_id,
            "email": email,
            "password": None,  # Google users have no password
            "first_name": first_name,
            "last_name": last_name,
            "referralCode": referral_code,
            "auth_provider": "google",
            # New fields with defaults
            "role": "user",
            "created_at": now,
            "updated_at": now,
            "is_active": True,
            "email_verified": True,  # Google accounts are pre-verified
            "account_status": "active",
            "last_login_at": now,
            "last_seen_at": now,
            "phone": None,
            "timezone": "UTC",
            "language": "en",
        })

        PostHogService.track_signup(user_id, email=email, method="google")

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
            asyncio.ensure_future(notification_service.notify_admin_new_signup(
                email=email,
                first_name=first_name,
                last_name=last_name,
                auth_provider="google",
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
            "is_new_user": is_new_user,
        },
    }


@router.post("/login")
async def login(body: LoginRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    user = await db["users"].find_one({"email": body.email})
    if not user or not pwd_context.verify(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user_id = user.get("userId") or str(user["_id"])

    # Check if email is verified - ONLY block NEW users who haven't verified yet
    # Existing users (account_status == "active") can login even if email not verified
    if (not user.get("email_verified") and
        user.get("auth_provider") != "google" and
        user.get("account_status") == "pending_verification"):

        # This is a NEW user who just signed up and hasn't verified yet
        # Generate and send a new verification code
        verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
        verification_code_expires = datetime.utcnow() + timedelta(minutes=15)

        await db["users"].update_one(
            {"email": body.email},
            {
                "$set": {
                    "verification_code": verification_code,
                    "verification_code_expires": verification_code_expires,
                    "updated_at": datetime.utcnow(),
                }
            }
        )

        # Send new verification email
        try:
            import asyncio
            asyncio.ensure_future(email_service.send_email(
                to_email=body.email,
                subject="Verify your URI Social account",
                template_name="email_verification",
                template_vars={
                    "first_name": user.get("first_name") or "there",
                    "verification_code": verification_code,
                    "expires_in": "15 minutes",
                }
            ))
        except Exception as e:
            print(f"⚠️ Verification email failed for {body.email}: {e}")

        raise HTTPException(
            status_code=403,
            detail="Please verify your email before logging in. We've sent a new verification code to your inbox."
        )

    # Update last_login_at and last_seen_at
    await db["users"].update_one(
        {"email": body.email},
        {"$set": {"last_login_at": datetime.utcnow(), "last_seen_at": datetime.utcnow()}}
    )

    PostHogService.track_login(user_id, email=user["email"], method="email")
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
            "emailVerified": user.get("email_verified", False),
        },
    }


# ==================== Email Verification Endpoints ====================

class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Verify user's email with the code sent to their inbox."""
    user = await db["users"].find_one({"email": body.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Check if already verified
    if user.get("email_verified"):
        raise HTTPException(status_code=400, detail="Email is already verified.")

    # Check verification code
    if user.get("verification_code") != body.code:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # Check if code has expired
    if user.get("verification_code_expires") and user["verification_code_expires"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new one.")

    user_id = user.get("userId") or str(user["_id"])
    now = datetime.utcnow()

    # Check if this is a NEW user (just signed up) or EXISTING user
    is_new_user = user.get("account_status") == "pending_verification"

    # Update user as verified
    await db["users"].update_one(
        {"email": body.email},
        {
            "$set": {
                "email_verified": True,
                "is_active": True,
                "account_status": "active",
                "last_login_at": now,
                "updated_at": now,
            },
            "$unset": {
                "verification_code": "",
                "verification_code_expires": "",
            }
        }
    )

    # ONLY activate trial for NEW users, NOT for existing users
    trial_status = None
    if is_new_user:
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

        # Send welcome email ONLY for new users
        try:
            import asyncio
            asyncio.ensure_future(notification_service.notify_signup(
                user_id=user_id,
                email=body.email,
                first_name=user.get("first_name", ""),
                trial_days=trial_status.get("days_remaining", 3) if trial_status else 0,
                trial_credits=trial_status.get("trial_credits", 10) if trial_status else 0,
            ))
            asyncio.ensure_future(notification_service.notify_admin_new_signup(
                email=body.email,
                first_name=user.get("first_name", ""),
                last_name=user.get("last_name", ""),
                auth_provider="email",
            ))
        except Exception as e:
            print(f"⚠️ Welcome email failed for {user_id}: {e}")
    else:
        # For existing users, just log the verification
        print(f"✅ Existing user {user_id} verified their email. Credits and subscription preserved.")

    # Generate JWT token
    token = sign_jwt(user_id, user["email"], user.get("first_name", ""), user.get("last_name", ""))

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Email verified successfully. Welcome to URI Social!",
        "responseData": {
            "accessToken": token,
            "userId": user_id,
            "email": user["email"],
            "firstName": user.get("first_name", ""),
            "lastName": user.get("last_name", ""),
            "trial": trial_status,
        },
    }


@router.post("/resend-verification")
async def resend_verification(body: ResendVerificationRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Resend verification code to user's email."""
    user = await db["users"].find_one({"email": body.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Check if already verified
    if user.get("email_verified"):
        raise HTTPException(status_code=400, detail="Email is already verified.")

    # Generate new verification code
    verification_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    verification_code_expires = datetime.utcnow() + timedelta(minutes=15)

    await db["users"].update_one(
        {"email": body.email},
        {
            "$set": {
                "verification_code": verification_code,
                "verification_code_expires": verification_code_expires,
                "updated_at": datetime.utcnow(),
            }
        }
    )

    # Send verification email
    try:
        import asyncio
        asyncio.ensure_future(email_service.send_email(
            to_email=body.email,
            subject="Verify your URI Social account",
            template_name="email_verification",
            template_vars={
                "first_name": user.get("first_name") or "there",
                "verification_code": verification_code,
                "expires_in": "15 minutes",
            }
        ))
    except Exception as e:
        print(f"⚠️ Verification email resend failed for {body.email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to send verification email. Please try again.")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Verification code sent to your email.",
        "responseData": {
            "email": body.email,
        },
    }


# ==================== Password Management Endpoints ====================

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Send password reset code to user's email."""
    user = await db["users"].find_one({"email": body.email})
    if not user:
        # Don't reveal if email exists or not (security best practice)
        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": "If an account exists with this email, a password reset code has been sent.",
            "responseData": {},
        }

    # Don't allow password reset for Google OAuth users
    if user.get("auth_provider") == "google":
        raise HTTPException(
            status_code=400,
            detail="This account uses Google sign-in. Please use Google to log in."
        )

    # Generate 6-digit reset code
    reset_code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    reset_code_expires = datetime.utcnow() + timedelta(minutes=15)

    await db["users"].update_one(
        {"email": body.email},
        {
            "$set": {
                "password_reset_code": reset_code,
                "password_reset_code_expires": reset_code_expires,
                "updated_at": datetime.utcnow(),
            }
        }
    )

    # Send password reset email
    try:
        import asyncio
        asyncio.ensure_future(email_service.send_email(
            to_email=body.email,
            subject="Reset your URI Social password",
            template_name="password_reset",
            template_vars={
                "first_name": user.get("first_name") or "there",
                "reset_code": reset_code,
                "expires_in": "15 minutes",
            }
        ))
    except Exception as e:
        print(f"⚠️ Password reset email failed for {body.email}: {e}")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "If an account exists with this email, a password reset code has been sent.",
        "responseData": {},
    }


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Reset password using the code sent to email."""
    user = await db["users"].find_one({"email": body.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Check reset code
    if user.get("password_reset_code") != body.code:
        raise HTTPException(status_code=400, detail="Invalid reset code.")

    # Check if code has expired
    if user.get("password_reset_code_expires") and user["password_reset_code_expires"] < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new one.")

    # Hash new password
    hashed_password = pwd_context.hash(body.new_password)

    # Update password and remove reset code
    await db["users"].update_one(
        {"email": body.email},
        {
            "$set": {
                "password": hashed_password,
                "updated_at": datetime.utcnow(),
            },
            "$unset": {
                "password_reset_code": "",
                "password_reset_code_expires": "",
            }
        }
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Password reset successfully. You can now log in with your new password.",
        "responseData": {},
    }


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    authorization: str = Header(None)
):
    """Change password for authenticated user."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")

    # Extract user from JWT token
    try:
        import jwt
        token = authorization.replace("Bearer ", "")
        payload = jwt.decode(token, settings.AUTHJWT_SECRET_KEY, algorithms=["HS256"])
        claims = payload.get("claims", {})
        user_id = claims.get("userId")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token.")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token.")

    user = await db["users"].find_one({"userId": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Don't allow password change for Google OAuth users
    if user.get("auth_provider") == "google":
        raise HTTPException(
            status_code=400,
            detail="This account uses Google sign-in and doesn't have a password."
        )

    # Verify old password
    if not pwd_context.verify(body.old_password, user["password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    # Hash new password
    hashed_password = pwd_context.hash(body.new_password)

    # Update password
    await db["users"].update_one(
        {"userId": user_id},
        {
            "$set": {
                "password": hashed_password,
                "updated_at": datetime.utcnow(),
            }
        }
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Password changed successfully.",
        "responseData": {},
    }
