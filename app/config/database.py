"""
Database configuration module for API key authentication.
Re-exports the database connections from the main database module.
"""
from app.database import get_db, get_sdk_gateway_db


async def get_database():
    """
    Get primary database instance.
    Wrapper around the main get_db function.
    """
    return get_db()


async def get_sdk_gateway_database():
    """
    Get SDK Gateway database instance for API key authentication.
    """
    return get_sdk_gateway_db()
