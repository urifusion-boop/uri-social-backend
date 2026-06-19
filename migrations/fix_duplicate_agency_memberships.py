"""
Migration: fix duplicate active agency memberships.

Before this fix (see AgencyService._holds_other_active_agency), accepting an
invite into a second agency silently created a SECOND "active" agency_members
row for the same user_id, instead of staying "invited". Because
get_agency_for_user does an unordered find_one, the user's dashboard kept
resolving to whichever agency Mongo happened to return first — usually their
original one — while the inviting admin's Members tab showed them as active.

This is a one-off cleanup for rows created before the fix. For every user_id
with more than one "active" agency_members row:
  - Keep the row for the agency they OWN (agencies.owner_user_id == user_id),
    if any.
  - Otherwise keep the oldest row (by joined_at, falling back to created_at).
  - Demote every other active row to status="invited" (joined_at cleared) so
    the user shows up as a pending invite the admin can re-send or remove.

Idempotent and safe to re-run. Defaults to a live run; pass --dry-run to only
print what would change.

Run:  python -m migrations.fix_duplicate_agency_memberships [--dry-run]
"""

import asyncio
import sys
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

AGENCIES = "agencies"
AGENCY_MEMBERS = "agency_members"


async def migrate(dry_run: bool = False):
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB]

    print("=" * 80)
    print(f"DUPLICATE AGENCY MEMBERSHIP CLEANUP {'(DRY RUN)' if dry_run else '(LIVE)'}")
    print("=" * 80)

    # Group active memberships by user_id
    by_user = {}
    async for m in db[AGENCY_MEMBERS].find({"status": "active", "user_id": {"$ne": None}}):
        by_user.setdefault(m["user_id"], []).append(m)

    duplicates = {uid: rows for uid, rows in by_user.items() if len(rows) > 1}
    print(f"\nUsers with more than one active agency membership: {len(duplicates)}\n")

    demoted = 0
    for user_id, rows in duplicates.items():
        owned_agency_ids = set()
        async for a in db[AGENCIES].find(
            {"agency_id": {"$in": [r["agency_id"] for r in rows]}, "owner_user_id": user_id},
            {"agency_id": 1},
        ):
            owned_agency_ids.add(a["agency_id"])

        owned_rows = [r for r in rows if r["agency_id"] in owned_agency_ids]
        if owned_rows:
            keep = owned_rows[0]
        else:
            keep = min(rows, key=lambda r: r.get("joined_at") or r.get("created_at") or datetime.max)

        print(f"user_id={user_id}: keeping agency_id={keep['agency_id']} (member_id={keep['agency_member_id']})")
        for r in rows:
            if r["_id"] == keep["_id"]:
                continue
            print(f"  -> demoting agency_id={r['agency_id']} (member_id={r['agency_member_id']}) to 'invited'")
            if not dry_run:
                await db[AGENCY_MEMBERS].update_one(
                    {"_id": r["_id"]},
                    {"$set": {"status": "invited", "joined_at": None, "updated_at": datetime.utcnow()}},
                )
            demoted += 1

    print(f"\n{'Would demote' if dry_run else 'Demoted'} {demoted} duplicate membership row(s).")
    print("Migration complete." if not dry_run else "Dry run complete — no changes made.")


if __name__ == "__main__":
    asyncio.run(migrate(dry_run="--dry-run" in sys.argv))
