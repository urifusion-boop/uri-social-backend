#!/usr/bin/env python3
"""Give saved video drafts the network as their `platform` ("instagram_reels" -> "instagram").

Video drafts saved from the video screens used to carry the screen's target name ("instagram_reels",
"facebook_reels") as their `platform`. Everything that posts or checks a draft looks the network up by
`platform` (the account connections, the Library's "is it connected" check, the approval and scheduling
path), so those drafts could not be posted from the Content Library. New drafts are saved correctly now
(save_video_draft); this fixes the ones already saved.

Only `platform` changes, and only on video drafts. `platforms` (the name the screen sent) is left alone.

    python scripts/backfill_video_draft_platform.py            # dry run: shows what would change
    python scripts/backfill_video_draft_platform.py --apply    # does it

It uses the database named in this environment's settings (MONGODB_*), so run it with the target
environment's variables set, and check the host it prints before using --apply.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_db  # noqa: E402

TARGETS = {"instagram_reels": "instagram", "facebook_reels": "facebook"}


async def main(apply: bool) -> int:
    col = get_db()["content_drafts"]
    for old, new in TARGETS.items():
        query = {"media_type": "video", "platform": old}
        count = await col.count_documents(query)
        print(f"{old} -> {new}: {count} video draft(s)")
        if apply and count:
            result = await col.update_many(query, {"$set": {"platform": new}})
            print(f"  updated {result.modified_count}")
    if not apply:
        print("\nDry run: nothing changed. Re-run with --apply to update them.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the change (default is a dry run)")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
