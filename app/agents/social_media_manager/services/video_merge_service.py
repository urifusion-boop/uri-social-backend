import asyncio
import os
import subprocess
import tempfile
from typing import List

import httpx

from app.utils.cloudinary_upload import upload_bytes


class VideoMergeService:

    @staticmethod
    async def merge_clips(clip_urls: List[str]) -> str:
        """Download clips, concatenate with ffmpeg, upload merged video. Returns Cloudinary URL."""
        async with httpx.AsyncClient(timeout=120) as client:
            responses = await asyncio.gather(*[client.get(url) for url in clip_urls])

        clip_bytes_list = [r.content for r in responses]

        loop = asyncio.get_running_loop()
        merged_bytes = await loop.run_in_executor(
            None,
            lambda: VideoMergeService._ffmpeg_concat(clip_bytes_list),
        )

        return await upload_bytes(
            merged_bytes,
            folder="uri-social/merged-videos",
            resource_type="video",
        )

    @staticmethod
    def _ffmpeg_concat(clips: List[bytes]) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, data in enumerate(clips):
                p = os.path.join(tmp, f"clip_{i}.mp4")
                with open(p, "wb") as f:
                    f.write(data)
                paths.append(p)

            list_path = os.path.join(tmp, "list.txt")
            with open(list_path, "w") as f:
                for p in paths:
                    f.write(f"file '{p}'\n")

            out_path = os.path.join(tmp, "merged.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path],
                check=True,
                capture_output=True,
            )

            with open(out_path, "rb") as f:
                return f.read()
