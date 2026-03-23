from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext

from app.core.auth_handler import sign_jwt
from app.database import get_db
from app.dependencies import get_db_dependency
from app.domain.responses.uri_response import UriResponse

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

    # Generate userId from ObjectId
    from bson import ObjectId
    import secrets
    user_object_id = ObjectId()
    user_id = str(user_object_id)

    # Generate unique referral code
    referral_code = secrets.token_urlsafe(8)

    result = await db["users"].insert_one({
        "_id": user_object_id,
        "userId": user_id,
        "email": body.email,
        "password": hashed,
        "first_name": body.first_name,
        "last_name": body.last_name,
        "referralCode": referral_code,
    })
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
        },
    }


@router.post("/login")
async def login(body: LoginRequest, db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    user = await db["users"].find_one({"email": body.email})
    if not user or not pwd_context.verify(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user_id = str(user["_id"])
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
