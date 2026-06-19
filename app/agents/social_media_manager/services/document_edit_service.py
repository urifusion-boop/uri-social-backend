"""
Document Edit Service - Undo/Redo and Edit History

Tracks all edits to layered documents for:
- Undo/Redo functionality (50 operations)
- Edit history/version control
- Analytics (which features users actually use)
- Audit trail

Each edit is stored as a reversible operation with before/after state.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
import uuid


class DocumentEditService:
    """Service for tracking and managing document edits"""

    MAX_UNDO_STACK = 50  # Keep last 50 operations in memory

    @staticmethod
    async def record_edit(
        draft_id: str,
        user_id: str,
        edit_type: str,
        layer_id: Optional[str],
        before_state: Dict[str, Any],
        after_state: Dict[str, Any],
        db: AsyncIOMotorDatabase
    ) -> str:
        """
        Record an edit operation to the database

        Args:
            draft_id: ID of the content draft being edited
            user_id: ID of the user making the edit
            edit_type: Type of edit (text_change, move, resize, color, visibility, etc.)
            layer_id: ID of the layer being edited (None for document-level changes)
            before_state: State before the edit
            after_state: State after the edit
            db: Database connection

        Returns:
            Edit ID
        """
        edit_id = str(uuid.uuid4())

        edit_doc = {
            "id": edit_id,
            "draft_id": draft_id,
            "user_id": user_id,
            "edit_type": edit_type,
            "layer_id": layer_id,
            "before_state": before_state,
            "after_state": after_state,
            "created_at": datetime.utcnow(),
            "undone": False  # Track if this edit was undone
        }

        await db["document_edits"].insert_one(edit_doc)

        print(f"[EditTracking] Recorded {edit_type} edit for draft {draft_id[:8]}")

        return edit_id

    @staticmethod
    async def get_edit_history(
        draft_id: str,
        db: AsyncIOMotorDatabase,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get edit history for a draft

        Args:
            draft_id: Draft ID
            db: Database connection
            limit: Maximum number of edits to return

        Returns:
            List of edit operations, newest first
        """
        cursor = db["document_edits"].find(
            {"draft_id": draft_id},
            {"_id": 0}
        ).sort("created_at", -1).limit(limit)

        edits = await cursor.to_list(length=limit)
        return edits

    @staticmethod
    async def apply_undo(
        draft_id: str,
        document: Dict[str, Any],
        db: AsyncIOMotorDatabase
    ) -> Optional[Dict[str, Any]]:
        """
        Undo the last edit

        Args:
            draft_id: Draft ID
            document: Current document state
            db: Database connection

        Returns:
            Updated document with last edit undone, or None if nothing to undo
        """
        # Find the last un-undone edit
        last_edit = await db["document_edits"].find_one(
            {"draft_id": draft_id, "undone": False},
            {"_id": 0},
            sort=[("created_at", -1)]
        )

        if not last_edit:
            return None  # Nothing to undo

        # Apply before_state
        before_state = last_edit.get("before_state", {})
        layer_id = last_edit.get("layer_id")

        if layer_id:
            # Layer-level undo
            from .layered_document_service import LayeredDocumentService
            document = LayeredDocumentService.update_layer(
                document,
                layer_id,
                before_state
            )
        else:
            # Document-level undo
            document.update(before_state)

        # Mark edit as undone
        await db["document_edits"].update_one(
            {"id": last_edit["id"]},
            {"$set": {"undone": True, "undone_at": datetime.utcnow()}}
        )

        print(f"[EditTracking] Undid {last_edit.get('edit_type')} edit")

        return document

    @staticmethod
    async def apply_redo(
        draft_id: str,
        document: Dict[str, Any],
        db: AsyncIOMotorDatabase
    ) -> Optional[Dict[str, Any]]:
        """
        Redo the last undone edit

        Args:
            draft_id: Draft ID
            document: Current document state
            db: Database connection

        Returns:
            Updated document with edit reapplied, or None if nothing to redo
        """
        # Find the last undone edit
        last_undone = await db["document_edits"].find_one(
            {"draft_id": draft_id, "undone": True},
            {"_id": 0},
            sort=[("undone_at", -1)]
        )

        if not last_undone:
            return None  # Nothing to redo

        # Apply after_state
        after_state = last_undone.get("after_state", {})
        layer_id = last_undone.get("layer_id")

        if layer_id:
            # Layer-level redo
            from .layered_document_service import LayeredDocumentService
            document = LayeredDocumentService.update_layer(
                document,
                layer_id,
                after_state
            )
        else:
            # Document-level redo
            document.update(after_state)

        # Mark edit as not undone
        await db["document_edits"].update_one(
            {"id": last_undone["id"]},
            {"$set": {"undone": False}, "$unset": {"undone_at": ""}}
        )

        print(f"[EditTracking] Redid {last_undone.get('edit_type')} edit")

        return document

    @staticmethod
    async def get_edit_analytics(
        user_id: Optional[str],
        db: AsyncIOMotorDatabase,
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Get analytics about edit patterns

        Args:
            user_id: Specific user ID, or None for all users
            db: Database connection
            days: Number of days to analyze

        Returns:
            Edit statistics
        """
        from datetime import timedelta

        since = datetime.utcnow() - timedelta(days=days)

        query = {"created_at": {"$gte": since}}
        if user_id:
            query["user_id"] = user_id

        # Aggregate by edit type
        pipeline = [
            {"$match": query},
            {"$group": {
                "_id": "$edit_type",
                "count": {"$sum": 1}
            }},
            {"$sort": {"count": -1}}
        ]

        results = await db["document_edits"].aggregate(pipeline).to_list(length=100)

        # Format results
        analytics = {
            "total_edits": sum(r["count"] for r in results),
            "by_type": {r["_id"]: r["count"] for r in results},
            "period_days": days
        }

        return analytics

    @staticmethod
    async def cleanup_old_edits(
        db: AsyncIOMotorDatabase,
        days: int = 30
    ) -> int:
        """
        Clean up edit history older than specified days

        Args:
            db: Database connection
            days: Keep edits from last N days

        Returns:
            Number of edits deleted
        """
        from datetime import timedelta

        cutoff_date = datetime.utcnow() - timedelta(days=days)

        result = await db["document_edits"].delete_many({
            "created_at": {"$lt": cutoff_date}
        })

        print(f"[EditTracking] Cleaned up {result.deleted_count} old edits")

        return result.deleted_count
