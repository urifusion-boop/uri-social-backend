from typing import Generator
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db


def get_db_dependency() -> Generator[AsyncIOMotorDatabase, None, None]:
    db = get_db()
    try:
        yield db
    finally:
        pass
