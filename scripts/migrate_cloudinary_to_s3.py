"""
Migrate existing Cloudinary-hosted assets to S3.

PREREQUISITE: Cloudinary's shared account must be reachable again (even
temporarily) before this can run at all — confirmed live 2026-09-30 that
delivery is blocked account-wide (x-cld-error: "df8ckaeam cannot be
accessed via this endpoint"), which blocks downloads too, not just new
uploads. Run this once that's resolved; every URL below is a real GET
against Cloudinary's own delivery domain.

Scans every collection in the database, recursively, for any field — a
plain string, or nested inside a dict/list, at any depth — whose value is
a Cloudinary delivery URL. No collection or field is hardcoded, so a font
URL buried in brand_profiles.primary_custom_fonts[].url is found the same
way a top-level brand_profiles.logo_url is. Each one is downloaded,
re-uploaded to S3 under the SAME folder/key path Cloudinary already used
(so the app's existing folder organisation — uri-social/logos/...,
uri-social/custom-fonts/..., etc. — carries over unchanged), and that
exact field is updated in place ($set on its dot-notation path — never a
full-document replace, so nothing else in the document can be clobbered
by a concurrent write elsewhere).

Dry-run by default — prints every (collection, document _id, field path,
URL) it would touch without downloading or writing anything. Pass
--execute to actually migrate.

Idempotent: a URL that's already an S3 URL (not Cloudinary) never
matches, so a partial/interrupted run can simply be re-run — it will
just find fewer Cloudinary URLs left to migrate each time.

Run from the project root:
  python -m scripts.migrate_cloudinary_to_s3            # dry run — review first
  python -m scripts.migrate_cloudinary_to_s3 --execute   # for real
"""
import argparse
import asyncio
import re

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.utils.s3_upload import _EXT_TO_CONTENT_TYPE, _S3_BUCKET, _S3_REGION, _s3_client

CLOUDINARY_URL_RE = re.compile(r"^https://res\.cloudinary\.com/[^/]+/(?:image|video|raw)/upload/")

# Collections that are pure operational/system data — never hold a media
# URL, and some are large — skipped so the scan doesn't waste time on them.
SKIP_COLLECTIONS = {"scheduled_job_locks", "notification_daily_claims", "pending_instagram_connections"}


def _key_from_cloudinary_url(url: str) -> str:
    """Reuses Cloudinary's own folder/public_id path as the S3 key,
    stripping only the res.cloudinary.com/<cloud>/<resource_type>/upload/
    [v<version>/] prefix Cloudinary adds."""
    m = re.match(r"^https://res\.cloudinary\.com/[^/]+/(?:image|video|raw)/upload/(?:v\d+/)?(.+)$", url)
    return m.group(1) if m else url.rsplit("/", 1)[-1]


async def _migrate_one_url(client: httpx.AsyncClient, url: str) -> str | None:
    resp = await client.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"    ⚠️  download failed ({resp.status_code}): {url}")
        return None
    key = _key_from_cloudinary_url(url)
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "bin"
    content_type = _EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream")
    _s3_client.put_object(Bucket=_S3_BUCKET, Key=key, Body=resp.content, ContentType=content_type)
    return f"https://{_S3_BUCKET}.s3.{_S3_REGION}.amazonaws.com/{key}"


def _find_cloudinary_urls(value, path=""):
    """Recursively walks a document yielding (mongo_dot_path, url) for
    every Cloudinary URL found. Paths use Mongo's own dot notation
    (list index as a bare number, e.g. "primary_custom_fonts.0.url") so
    the result can be used directly as a $set key with no translation."""
    if isinstance(value, str):
        if CLOUDINARY_URL_RE.match(value):
            yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k == "_id":
                continue
            yield from _find_cloudinary_urls(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _find_cloudinary_urls(v, f"{path}.{i}" if path else str(i))


async def run(execute: bool) -> None:
    db = AsyncIOMotorClient(settings.MONGODB_URI)[settings.MONGODB_DB]
    collections = [c for c in await db.list_collection_names() if c not in SKIP_COLLECTIONS]

    total_found = 0
    total_migrated = 0
    total_failed = 0

    async with httpx.AsyncClient() as client:
        for coll_name in collections:
            coll = db[coll_name]
            coll_hits = 0
            async for doc in coll.find({}):
                hits = list(_find_cloudinary_urls(doc))
                if not hits:
                    continue
                coll_hits += len(hits)
                updates = {}
                for field_path, url in hits:
                    total_found += 1
                    print(f"[{coll_name}] {doc.get('_id')} :: {field_path} -> {url}")
                    if not execute:
                        continue
                    new_url = await _migrate_one_url(client, url)
                    if new_url:
                        updates[field_path] = new_url
                        total_migrated += 1
                        print(f"    ✅ -> {new_url}")
                    else:
                        total_failed += 1
                if execute and updates:
                    await coll.update_one({"_id": doc["_id"]}, {"$set": updates})
            if coll_hits:
                print(f"  ── {coll_name}: {coll_hits} URL(s) found ──")

    print()
    print(f"Found: {total_found}")
    if execute:
        print(f"Migrated: {total_migrated}")
        print(f"Failed: {total_failed}")
    else:
        print("Dry run only — nothing downloaded or written. Pass --execute to actually migrate.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually migrate (default is dry-run)")
    args = parser.parse_args()
    asyncio.get_event_loop().run_until_complete(run(args.execute))
