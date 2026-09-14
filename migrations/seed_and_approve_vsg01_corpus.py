"""
One-off: seed VSG-01's 12 ad-format corpus records and approve them.

Run this from wherever you have real network access to the dev
DocumentDB cluster (inside a running ECS task, a bastion, VPN — this
machine could not reach it directly, hence handing you this script).

Reuses the app's own settings (app.core.config.settings.MONGODB_URI/
MONGODB_DB) rather than a hardcoded connection string, so it resolves the
DB connection exactly the way the real app does in whatever environment
you run it from.

Idempotent — safe to re-run: records already ingested are left alone
(not re-ingested), and only records not yet approved get approved.

--approved-by is required, not defaulted to a placeholder — the whole
point of this step is a *real* human reviewing and approving these
records (corpus.py's own established rule: ingestion can never
self-approve). Whoever runs this is the reviewer of record.

Usage:
    python migrations/seed_and_approve_vsg01_corpus.py --approved-by "you@example.com"
    python migrations/seed_and_approve_vsg01_corpus.py --approved-by "you@example.com" --dry-run
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.agents.jane_ads.entities import StrategyStatus  # noqa: E402
from app.agents.jane_ads.store import MongoStrategyStore  # noqa: E402
from app.agents.jane_ads.vsg01_corpus_seed import build_vsg01_strategies  # noqa: E402


async def run(approved_by: str, dry_run: bool) -> None:
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DB]
    store = MongoStrategyStore(db)

    print(f"Connecting to {settings.MONGODB_DB} …")
    await store.ensure_indexes()

    strategies = build_vsg01_strategies()
    print(f"\n{len(strategies)} VSG-01 format records to check:")

    to_ingest = []
    to_approve = []
    for s in strategies:
        existing = await store.get(s.strategy_id, s.version)
        if existing is None:
            to_ingest.append(s)
            to_approve.append(s)
            print(f"  {s.strategy_id}  NEW — will ingest as draft, then approve")
        elif existing.status is StrategyStatus.APPROVED:
            print(f"  {s.strategy_id}  already approved — leaving as-is")
        else:
            to_approve.append(s)
            print(f"  {s.strategy_id}  ingested (status={existing.status.value}) — will approve")

    if dry_run:
        print(f"\n--dry-run: would ingest {len(to_ingest)}, approve {len(to_approve)}. No changes made.")
        return

    for s in to_ingest:
        await store.ingest(s)
    for s in to_approve:
        await store.approve(s.strategy_id, s.version, approved_by=approved_by)

    approved = await store.fetch_approved()
    vsg01_ids = {s.strategy_id for s in strategies}
    live = [r for r in approved if r.strategy_id in vsg01_ids]

    print(f"\nDone. {len(live)}/{len(strategies)} VSG-01 format records are now APPROVED and live:")
    for r in sorted(live, key=lambda r: r.strategy_id):
        print(f"  {r.strategy_id}  {r.claim[:70]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-by", required=True, help="Who is approving these records (name or email)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing anything")
    args = parser.parse_args()
    asyncio.run(run(args.approved_by, args.dry_run))
