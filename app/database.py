from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from urllib.parse import quote_plus
from app.core.config import settings

# Primary database client (existing - for main app data)
client: Optional[AsyncIOMotorClient] = None

# SDK Gateway database client (NEW - for API key authentication only)
sdk_gateway_client: Optional[AsyncIOMotorClient] = None


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
        client = AsyncIOMotorClient(settings.MONGODB_URI)
        print(f"✅ Connected to primary database: {database_name}")


def connect_to_sdk_gateway_db() -> None:
    """Connect to SDK Gateway MongoDB database for API key authentication"""
    global sdk_gateway_client
    
    if not settings.SDK_GATEWAY_MONGODB_URI:
        print("⚠️  SDK_GATEWAY_MONGODB_URI not configured. API key authentication will not work.")
        return
    
    sdk_gateway_client = AsyncIOMotorClient(settings.SDK_GATEWAY_MONGODB_URI)
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
