import json
import re
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from urllib.parse import quote_plus
from app.core.config import settings

# Primary database client (existing - for main app data)
client: Optional[AsyncIOMotorClient] = None

# SDK Gateway database client (NEW - for API key authentication only)
sdk_gateway_client: Optional[AsyncIOMotorClient] = None

# mongodb://user:password@host/... — on a DocumentDB managed-rotation event
# only the password changes; scheme/user/host/db/query params are reused.
_MONGO_USERINFO_RE = re.compile(r"^(mongodb(?:\+srv)?://)([^:@/]+):([^@]+)@(.+)$", re.DOTALL)


def _rotated_docdb_password() -> Optional[str]:
    """Current DocumentDB master password straight from the AWS-managed
    rotation secret (via boto3's default chain → the task's own IAM role).
    None when managed rotation isn't configured (DOCDB_SECRET_ARN unset —
    local dev / static password) or the secret can't be read; callers then
    keep whatever password the configured URI already carries."""
    if not settings.DOCDB_SECRET_ARN:
        return None
    try:
        import boto3
        resp = boto3.client("secretsmanager").get_secret_value(SecretId=settings.DOCDB_SECRET_ARN)
        return json.loads(resp["SecretString"]).get("password") or None
    except Exception as e:  # unreachable secret, bad JSON, no perms — fail open
        print(f"⚠️  DocumentDB: could not read the rotated password from Secrets Manager "
              f"({e}); using the configured connection string as-is")
        return None


def with_current_docdb_password(uri: Optional[str]) -> Optional[str]:
    """Return `uri` with its password replaced by the current rotated value.
    A no-op when there is nothing to do — no managed rotation, no userinfo
    in the uri, or the secret is unreadable. This is what lets a task that
    starts AFTER a rotation still authenticate even when the new password
    was never propagated into the SSM parameter (the cause of a prod login
    outage: the SSM MONGODB_URL went stale, every fresh task failed auth on
    startup, and the in-process refresher below can't help a connection
    that never came up)."""
    if not uri:
        return uri
    pw = _rotated_docdb_password()
    if not pw:
        return uri
    m = _MONGO_USERINFO_RE.match(uri)
    if not m:
        return uri
    scheme, user, _old, rest = m.groups()
    return f"{scheme}{user}:{quote_plus(pw)}@{rest}"


def connect_to_mongo(database_name: str) -> None:
    """Connect to primary MongoDB database (existing behavior)"""
    global client

    if settings.DEV_ENV == "Development":
        if settings.MONGODB_USER and settings.MONGODB_PASSWORD:
            user = quote_plus(settings.MONGODB_USER)
            password = quote_plus(settings.MONGODB_PASSWORD)
            host = settings.MONGODB_HOST
            uri = f"mongodb://{user}:{password}@{host}/{database_name}?authSource=admin"
            print(f"Primary DB URI: {uri.replace(password, '*****')}")
        else:
            uri = settings.MONGODB_URI
            print(f"Primary DB URI: {uri}")
        client = AsyncIOMotorClient(uri)
    else:
        client = AsyncIOMotorClient(with_current_docdb_password(settings.MONGODB_URI))
        print(f"✅ Connected to primary database: {database_name}")


def connect_to_sdk_gateway_db() -> None:
    """Connect to SDK Gateway MongoDB database for API key authentication"""
    global sdk_gateway_client

    if not settings.SDK_GATEWAY_MONGODB_URI:
        print("⚠️  SDK_GATEWAY_MONGODB_URI not configured. API key authentication will not work.")
        return

    sdk_gateway_client = AsyncIOMotorClient(with_current_docdb_password(settings.SDK_GATEWAY_MONGODB_URI))
    print(f"✅ Connected to SDK Gateway database: {settings.SDK_GATEWAY_DB}")


def get_db() -> AsyncIOMotorDatabase:
    """Get primary database (existing behavior - no changes)"""
    if client is None:
        raise ConnectionError("Primary client is not connected.")
    return client.get_default_database()


def get_sdk_gateway_db() -> AsyncIOMotorDatabase:
    """Get SDK Gateway database for API key operations"""
    if sdk_gateway_client is None:
        raise ConnectionError("SDK Gateway client is not connected. Check SDK_GATEWAY_MONGODB_URI configuration.")
    return sdk_gateway_client[settings.SDK_GATEWAY_DB]
