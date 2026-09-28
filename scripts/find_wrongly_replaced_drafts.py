"""
Read-only diagnostic: find every content_drafts document the cron's now-
removed (user_id, platform) blanket dedup silently cancelled.

Targets the EXACT signature of that specific bug — error_message == the
literal string the dedup wrote — so this cannot pick up drafts cancelled
for a different, legitimate reason (e.g. approve_content's narrower
in-flight cancel, which uses a different message: "Superseded by a newer
scheduled post.").

Makes no writes. Safe to run against any environment; only prints a report.

Usage:
    python3 scripts/find_wrongly_replaced_drafts.py
"""
import asyncio

from app.database import get_db

BUG_ERROR_MESSAGE = "Superseded by a newer draft for the same platform."


async def main():
    db = get_db()
    cursor = db["content_drafts"].find(
        {"status": "replaced", "error_message": BUG_ERROR_MESSAGE}
    ).sort("updated_at", -1)
    docs = await cursor.to_list(length=None)

    if not docs:
        print("No drafts found with this bug's signature.")
        return

    print(f"Found {len(docs)} draft(s) wrongly cancelled by the dedup bug:\n")
    by_user = {}
    for d in docs:
        by_user.setdefault(d.get("user_id"), []).append(d)

    for user_id, drafts in by_user.items():
        print(f"user_id={user_id} — {len(drafts)} affected draft(s)")
        for d in sorted(drafts, key=lambda x: x.get("scheduled_date") or ""):
            content_preview = (d.get("content") or "")[:80].replace("\n", " ")
            print(
                f"  draft_id={d.get('id')} platform={d.get('platform')} "
                f"was_scheduled_for={d.get('scheduled_date')} "
                f"cancelled_at={d.get('updated_at')} "
                f"content=\"{content_preview}...\""
            )
        print()

    print(f"Total: {len(docs)} draft(s) across {len(by_user)} user(s).")
    print("\nNone of these were deleted — each still has its original content ")
    print("and can be re-approved/re-scheduled once you've reviewed the list.")


if __name__ == "__main__":
    asyncio.run(main())
