"""S3-backed replacement for app/utils/cloudinary_upload.py — same function
names and signatures (upload_bytes, upload_base64), same return contract (a
public HTTPS URL), so every existing call site works by only swapping the
import. Built after Cloudinary's shared account started blocking delivery
account-wide (confirmed live: every asset under that cloud, not just ours,
returned 401 "cannot be accessed via this endpoint") — moving onto S3 means
URI's own AWS account, not a third party, controls whether uploads stay
reachable.

S3_MEDIA_BUCKET must point at this environment's own bucket (uri-social-media-prod
here — never the dev bucket; keeping media physically separate per environment).
"""
import asyncio
import base64
import io
import os
import re
import uuid
from functools import partial

import boto3
from botocore.config import Config as _BotoConfig

_S3_BUCKET = os.environ.get("S3_MEDIA_BUCKET", "")
_S3_REGION = os.environ.get("AWS_REGION", "eu-west-1")

# Explicit SigV4 — botocore's default for this account/region otherwise fell back to
# legacy SigV2 presigned URLs, which (unlike SigV4's query-string scheme) bind
# Content-Type into the signature even when it's left out of Params, breaking
# presigned_put_url() the moment the browser's real file.type didn't match the
# (absent) signed value. SigV4 only binds headers actually listed as signed.
_s3_client = boto3.client("s3", region_name=_S3_REGION, config=_BotoConfig(signature_version="s3v4"))

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


def key_from_public_url(url: str) -> str:
    """The inverse of _public_url — returns "" for anything that isn't actually
    one of our own objects (a bucket this process didn't upload to), so a caller
    can refuse to sign a URL it doesn't recognise rather than quietly SSRF-ing an
    arbitrary host through generate_presigned_url."""
    prefix = _public_url("")
    return url[len(prefix):] if url.startswith(prefix) else ""


def presigned_download_url(url: str, filename: str, expires_in: int = 300) -> str:
    """A short-lived presigned GET for `url` that forces the browser to save it as
    `filename` instead of whatever name the S3 key happens to have — the
    replacement for Cloudinary's fl_attachment delivery-URL flag
    (cloudinaryDownload.util.ts), which doesn't exist for a plain S3 URL. Returns
    "" if `url` isn't one of our own S3 objects, so the caller can fall back to
    the plain URL — signing never touches the network, so an object that
    doesn't exist just 404s when fetched, same as any other broken link."""
    key = key_from_public_url(url)
    if not key:
        return ""
    safe_name = re.sub(r'[^A-Za-z0-9 ._-]', '', filename).strip() or "download"
    return _s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": _S3_BUCKET, "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
        },
        ExpiresIn=expires_in,
    )


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


def presigned_put_url(folder: str, public_id: str, ext: str = "mp4", expires_in: int = 900) -> dict:
    """A presigned PUT URL + the final public URL, for a browser to upload a large
    file straight to S3 — the replacement for Cloudinary's signed-chunked-upload
    params (cloudinary_upload_signature). A single PUT is simpler than a real
    resumable multipart upload (which would need three round trips — create/
    part-urls/complete — instead of one); not worth it unless dropped-connection
    resume on a large upload turns out to matter in practice.

    ContentType is deliberately left out of the signed params — a presigned URL
    signed with a specific ContentType rejects a PUT whose header doesn't match it
    byte-for-byte (SignatureDoesNotMatch), and the browser's real file.type varies
    by source (video/mp4, video/quicktime for a .mov, video/webm, ...). Leaving it
    unsigned lets the PUT declare whatever Content-Type the file actually is."""
    key = f"{folder}/{public_id}.{ext}"
    url = _s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": _S3_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )
    return {"put_url": url, "public_url": _public_url(key)}


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
    it)."""
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
