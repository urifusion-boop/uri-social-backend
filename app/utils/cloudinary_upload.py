import asyncio
import io
import os
from functools import partial

import cloudinary
import cloudinary.uploader

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
    secure=True,
)


async def upload_base64(data_url: str, folder: str = "uri-social") -> str:
    """Upload a base64 data URL to Cloudinary. Returns secure_url."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        partial(
            cloudinary.uploader.upload,
            data_url,
            folder=folder,
            resource_type="image",
        ),
    )
    return result["secure_url"]


async def upload_bytes(
    file_bytes: bytes,
    folder: str = "uri-social",
    resource_type: str = "image",
) -> str:
    """Upload raw bytes to Cloudinary. Returns secure_url."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        partial(
            cloudinary.uploader.upload,
            io.BytesIO(file_bytes),
            folder=folder,
            resource_type=resource_type,
        ),
    )
    return result["secure_url"]
