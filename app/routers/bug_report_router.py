from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.auth_bearer import JWTBearer
from app.dependencies import get_db_dependency

router = APIRouter(prefix="/bug-reports", tags=["Bug Reports"])


class BugReportRequest(BaseModel):
    category: str = Field(..., description="Category: ui | content | posting | billing | performance | other")
    title: str = Field(..., min_length=5, max_length=200)
    description: str = Field(..., min_length=10, max_length=5000)
    steps_to_reproduce: Optional[str] = Field(None, max_length=2000)
    page_url: Optional[str] = Field(None, max_length=500)
    browser_info: Optional[str] = Field(None, max_length=500)


def _get_user_id(token: dict) -> str:
    return token.get("claims", {}).get("userId", "")


@router.post("/report")
async def report_bug(
    body: BugReportRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    user_id = _get_user_id(token)
    if not user_id:
        return {"status": False, "responseCode": 401, "responseMessage": "Unauthorized"}

    report_id = f"bug_{user_id}_{int(datetime.utcnow().timestamp() * 1000)}"
    doc = {
        "report_id": report_id,
        "user_id": user_id,
        "category": body.category,
        "title": body.title,
        "description": body.description,
        "steps_to_reproduce": body.steps_to_reproduce,
        "page_url": body.page_url,
        "browser_info": body.browser_info,
        "status": "open",
        "created_at": datetime.utcnow(),
    }

    await db["bug_reports"].insert_one(doc)
    print(f"🐛 Bug report submitted | report_id={report_id} user_id={user_id} category={body.category} title={body.title!r}")

    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("report_id", report_id)
            scope.set_tag("category", body.category)
            scope.set_user({"id": user_id})
            scope.set_extra("title", body.title)
            scope.set_extra("description", body.description)
            scope.set_extra("steps_to_reproduce", body.steps_to_reproduce)
            scope.set_extra("page_url", body.page_url)
            scope.set_extra("browser_info", body.browser_info)
            sentry_sdk.capture_message(
                f"[Bug Report] {body.category.upper()}: {body.title}",
                level="warning",
            )
    except Exception as sentry_err:
        print(f"⚠️ Sentry capture failed (non-blocking): {sentry_err}")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Bug report submitted. Thank you for helping us improve!",
        "responseData": {"report_id": report_id},
    }


@router.get("/")
async def list_bug_reports(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category: Optional[str] = None,
    status: Optional[str] = None,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """List bug reports — returns own reports for regular users."""
    user_id = _get_user_id(token)
    if not user_id:
        return {"status": False, "responseCode": 401, "responseMessage": "Unauthorized"}

    query: dict = {"user_id": user_id}
    if category:
        query["category"] = category
    if status:
        query["status"] = status

    skip = (page - 1) * page_size
    total = await db["bug_reports"].count_documents(query)
    cursor = db["bug_reports"].find(query).sort("created_at", -1).skip(skip).limit(page_size)
    reports = []
    async for doc in cursor:
        doc.pop("_id", None)
        reports.append(doc)

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Bug reports retrieved.",
        "responseData": {"reports": reports, "total": total, "page": page, "page_size": page_size},
    }
