"""
Admin script: ingest the hand-seeded ad strategy corpus into Mongo.

    python3 -m app.scripts.import_ad_strategy_corpus URI_Ad_Strategy_Corpus_Seed_v1.xlsx
    python3 -m app.scripts.import_ad_strategy_corpus <file> --dry-run

Non-interactive by design so it can run in CI or a deploy hook. Rejected rows are
listed with their sheet row number so a seeder can fix the workbook directly; the
run exits non-zero if any row was rejected, so a bad sheet fails a pipeline loudly
rather than importing a partial corpus in silence.
"""
import argparse
import asyncio
import sys

from app.agents.jane_ads.corpus import import_rows, read_records_sheet
from app.agents.jane_ads.store import InMemoryStrategyStore, MongoStrategyStore
from app.core.config import settings
from app.database import connect_to_mongo, get_db


async def run(path: str, dry_run: bool) -> int:
    rows = read_records_sheet(path)

    if dry_run:
        store = InMemoryStrategyStore()
        print(f"DRY RUN — nothing will be written. Reading {path}")
    else:
        connect_to_mongo(settings.MONGODB_DB)
        store = MongoStrategyStore(get_db())
        await store.ensure_indexes()
        print(f"Importing {path} into {settings.MONGODB_DB}.jane_ads_strategies")

    report = await import_rows(rows, store)
    print(report.summary())

    if report.errors:
        print("\nRejected rows (fix these in the workbook):")
        for e in report.errors:
            print(f"  row {e.row:<5} {e.strategy_id:<12} {e.reason}")

    if not dry_run:
        print(f"\nCorpus now holds {await store.count()} strategies.")

    return 1 if report.errors else 0



async def watch(path: str, dry_run: bool, interval: float) -> int:
    """Re-import on every save, so seeding in the workbook lands in the corpus without
    anyone remembering to run the importer.

    Safe to leave running: the import upserts on (strategy_id, version), so a
    re-import is idempotent — it updates drafts in place and never duplicates. It
    also cannot approve anything (the store refuses), so a sheet edit can add and
    revise records but never push one live on its own.
    """
    import os

    print(f"Watching {path} — re-imports on change, Ctrl-C to stop.")
    last = None
    while True:
        try:
            stamp = os.path.getmtime(path)
        except OSError as e:
            print(f"  cannot stat {path}: {e}", flush=True)
            await asyncio.sleep(interval)
            continue
        if stamp != last:
            if last is not None:
                print(f"\n[{_clock()}] change detected", flush=True)
            await run(path, dry_run)
            last = stamp
        await asyncio.sleep(interval)


def _clock() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest the ad strategy corpus workbook.")
    ap.add_argument("path", help="Path to URI_Ad_Strategy_Corpus_Seed_v1.xlsx")
    ap.add_argument("--dry-run", action="store_true", help="Validate without writing")
    ap.add_argument("--watch", action="store_true",
                    help="Re-import whenever the workbook changes on disk")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Seconds between change checks when --watch is set")
    args = ap.parse_args()
    if args.watch:
        sys.exit(asyncio.run(watch(args.path, args.dry_run, args.interval)))
    sys.exit(asyncio.run(run(args.path, args.dry_run)))


if __name__ == "__main__":
    main()
