"""S3-backed replacement for app/utils/cloudinary_upload.py — same function
names and signatures (upload_bytes, upload_base64), same return contract (a
public HTTPS URL), so every existing call site works by only swapping the
import. Built after Cloudinary's shared account started blocking delivery
account-wide (confirmed live: every asset under that cloud, not just ours,
returned 401 "cannot be accessed via this endpoint") — moving onto S3 means
URI's own AWS account, not a third party, controls whether uploads stay
reachable.

dev-only for now (S3_MEDIA_BUCKET is the dev bucket) — not wired into any
aws/prod call site yet.
"""
import asyncio
import base64
import io
import os
import re
import uuid
from functools import partial

import boto3

_S3_BUCKET = os.environ.get("S3_MEDIA_BUCKET", "")
_S3_REGION = os.environ.get("AWS_REGION", "eu-west-1")

_s3_client = boto3.client("s3", region_name=_S3_REGION)

_EXT_TO_CONTENT_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "woff": "font/woff",
    "woff2": "font/woff2",
    "bin": "application/octet-stream",
}


def _public_url(key: str) -> str:
    return f"https://{_S3_BUCKET}.s3.{_S3_REGION}.amazonaws.com/{key}"


def _sniff_extension(file_bytes: bytes, resource_type: str) -> str:
    """Cloudinary auto-detects format from the bytes and needs no extension
    in its URLs; S3 needs a real extension in the key (for the right
    Content-Type, and so a browser/CDN treats it as the right kind of file).
    Checked by magic bytes rather than trusted from the caller, since every
    existing call site only ever passes resource_type ("image"/"video"/
    "raw"), never a specific format."""
    head = file_bytes[:16]
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:2] == b"<?" or head[:4] == b"<svg" or b"<svg" in file_bytes[:200]:
        return "svg"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OTTO":
        return "otf"
    if head[:4] == b"\x00\x01\x00\x00" or head[:4] == b"true":
        return "ttf"
    if head[:4] == b"wOFF":
        return "woff"
    if head[:4] == b"wOF2":
        return "woff2"
    if head[:3] == b"ID3" or head[:2] == b"\xff\xfb":
        return "mp3"
    # Sniff failed — fall back to a sane default for the declared resource
    # type rather than a bare "bin" that browsers won't render at all.
    return {"image": "png", "video": "mp4", "raw": "bin"}.get(resource_type, "bin")


async def upload_base64(data_url: str, folder: str = "uri-social") -> str:
    """Upload a base64 data URL to S3. Returns the public HTTPS URL — same
    contract as cloudinary_upload.upload_base64."""
    match = re.match(r"data:([^;]+);base64,(.+)", data_url, re.DOTALL)
    b64_body = match.group(2) if match else data_url
    file_bytes = base64.b64decode(b64_body)

    if match:
        declared_content_type = match.group(1)
        ext = declared_content_type.split("/")[-1] if "/" in declared_content_type else "bin"
        content_type = declared_content_type
    else:
        # Not every caller wraps its base64 in a proper data: URL — sniff
        # from the decoded bytes instead of trusting a declared type that
        # was never given.
        ext = _sniff_extension(file_bytes, "image")
        content_type = _EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream")

    key = f"{folder}/{uuid.uuid4().hex}.{ext}"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        partial(
            _s3_client.put_object,
            Bucket=_S3_BUCKET,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
        ),
    )
    return _public_url(key)


async def upload_bytes(
    file_bytes: bytes,
    folder: str = "uri-social",
    resource_type: str = "image",
    public_id: str | None = None,
) -> str:
    """Upload raw bytes to S3. Returns the public HTTPS URL — same contract
    as cloudinary_upload.upload_bytes."""
    ext = _sniff_extension(file_bytes, resource_type)
    key_name = public_id if public_id else uuid.uuid4().hex
    key = f"{folder}/{key_name}.{ext}"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        partial(
            _s3_client.put_object,
            Bucket=_S3_BUCKET,
            Key=key,
            Body=file_bytes,
            ContentType=_EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream"),
        ),
    )
    return _public_url(key)


async def upload_file_path(
    file_path: str,
    folder: str = "uri-social",
    resource_type: str = "video",
    public_id: str | None = None,
) -> str:
    """Upload a file already on local disk to S3, streaming it in chunks via
    boto3's upload_file — never reads the whole file into memory. Use this
    instead of upload_bytes for anything that didn't already need to be a
    Python bytes object for some other reason (downloaded media especially —
    stream the download straight to a temp file and hand this the path,
    rather than buffering it fully in memory just to immediately re-upload
    it). Confirmed live: a video-generation request doing exactly that
    (download a multi-MB clip into a `bytes`, then upload_bytes it straight
    back out) was large enough, combined with glibc not returning the
    memory to the OS afterward (containerised services commonly keep the
    per-thread malloc arena it was allocated in), that repeated requests
    ratcheted the process's memory up until the kernel OOM-killed a uvicorn
    worker — same final symptom as a real leak, without one actually being
    in the Python object graph."""
    with open(file_path, "rb") as f:
        head = f.read(200)
    ext = _sniff_extension(head, resource_type)
    key_name = public_id if public_id else uuid.uuid4().hex
    key = f"{folder}/{key_name}.{ext}"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        partial(
            _s3_client.upload_file,
            file_path,
            _S3_BUCKET,
            key,
            ExtraArgs={"ContentType": _EXT_TO_CONTENT_TYPE.get(ext, "application/octet-stream")},
        ),
    )
    return _public_url(key)
