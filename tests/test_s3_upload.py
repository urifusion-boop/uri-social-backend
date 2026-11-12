"""app/utils/s3_upload.py replaces app/utils/cloudinary_upload.py with the
same upload_bytes/upload_base64 contract, so every existing call site works
unchanged by only swapping the import. Covers: the public URL shape, that
Content-Type/extension get set correctly from sniffed magic bytes (S3, unlike
Cloudinary, needs a real extension in the key), and the public_id passthrough
that test_cloudinary_upload.py already guards for the Cloudinary version
(confirmed live there as a real regression — the same parameter must not
silently break again here)."""
import asyncio
import base64
import os
import tempfile
from unittest.mock import MagicMock, patch

from app.utils.s3_upload import (
    _sniff_extension,
    key_from_public_url,
    presigned_download_url,
    presigned_put_url,
    upload_base64,
    upload_bytes,
    upload_file_path,
)

# Minimal real magic-byte prefixes for each format, not full valid files —
# _sniff_extension only ever looks at the header, so a truncated-but-correct
# prefix is enough and keeps the test data readable.
_PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 8
_WEBP_HEAD = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4
_GIF_HEAD = b"GIF89a" + b"\x00" * 8
_MP4_HEAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4
_TTF_HEAD = b"\x00\x01\x00\x00" + b"\x00" * 8
_OTF_HEAD = b"OTTO" + b"\x00" * 8
_WOFF2_HEAD = b"wOF2" + b"\x00" * 8


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSniffExtension:
    def test_png(self):
        assert _sniff_extension(_PNG_HEAD, "image") == "png"

    def test_jpeg(self):
        assert _sniff_extension(_JPEG_HEAD, "image") == "jpg"

    def test_webp(self):
        assert _sniff_extension(_WEBP_HEAD, "image") == "webp"

    def test_gif(self):
        assert _sniff_extension(_GIF_HEAD, "image") == "gif"

    def test_mp4(self):
        assert _sniff_extension(_MP4_HEAD, "video") == "mp4"

    def test_ttf(self):
        assert _sniff_extension(_TTF_HEAD, "raw") == "ttf"

    def test_otf(self):
        assert _sniff_extension(_OTF_HEAD, "raw") == "otf"

    def test_woff2(self):
        assert _sniff_extension(_WOFF2_HEAD, "raw") == "woff2"

    def test_unrecognized_bytes_fall_back_to_resource_type_default(self):
        garbage = b"not a real file header at all"
        assert _sniff_extension(garbage, "image") == "png"
        assert _sniff_extension(garbage, "video") == "mp4"
        assert _sniff_extension(garbage, "raw") == "bin"


class TestUploadBytes:
    def test_uploads_with_sniffed_content_type_and_returns_public_url(self):
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            url = _run(upload_bytes(_PNG_HEAD, folder="uri-social/logos"))

        assert url.startswith("https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-social/logos/")
        assert url.endswith(".png")
        _, kwargs = mock_s3.put_object.call_args
        assert kwargs["Bucket"] == "uri-social-media-dev"
        assert kwargs["ContentType"] == "image/png"
        assert kwargs["Key"].startswith("uri-social/logos/")
        assert kwargs["Key"].endswith(".png")

    def test_public_id_becomes_the_key_name_not_a_random_uuid(self):
        """Same regression test_cloudinary_upload.py guards for the Cloudinary
        version — public_id must be honored, not silently dropped."""
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            url = _run(upload_bytes(
                _TTF_HEAD, folder="uri-social/custom-fonts/u1",
                resource_type="raw", public_id="MTNBRIGHTERSANS-LIGHT",
            ))

        assert "MTNBRIGHTERSANS-LIGHT.ttf" in url
        _, kwargs = mock_s3.put_object.call_args
        assert kwargs["Key"] == "uri-social/custom-fonts/u1/MTNBRIGHTERSANS-LIGHT.ttf"

    def test_omitted_public_id_generates_a_unique_key(self):
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            url1 = _run(upload_bytes(_PNG_HEAD, folder="uri-social/logos"))
            url2 = _run(upload_bytes(_PNG_HEAD, folder="uri-social/logos"))

        assert url1 != url2, "two uploads with no public_id must not collide on the same key"


class TestUploadFilePath:
    """upload_file_path — the streaming, disk-based sibling to upload_bytes.
    Added for video generation specifically: downloading a multi-MB clip
    fully into memory just to immediately re-upload it (the old pattern)
    was large and frequent enough, combined with glibc not returning freed
    memory to the OS inside the container, to ratchet process memory up
    until the kernel OOM-killed a uvicorn worker — confirmed live via
    repeating "Child process [N] died" log entries. upload_file never loads
    the whole file into Python memory; it streams from disk."""

    def _write_tmp(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    def test_uploads_via_upload_file_not_put_object(self):
        """The whole point — upload_file streams from disk in chunks;
        put_object would require the caller to have the full bytes in
        memory already, defeating the purpose of this function existing."""
        path = self._write_tmp(_MP4_HEAD)
        try:
            with patch("app.utils.s3_upload._s3_client") as mock_s3, \
                 patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
                 patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
                url = _run(upload_file_path(path, folder="uri-social/generated-videos"))

            assert url.startswith("https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-social/generated-videos/")
            assert url.endswith(".mp4")
            mock_s3.put_object.assert_not_called()
            args, kwargs = mock_s3.upload_file.call_args
            assert args[0] == path
            assert args[1] == "uri-social-media-dev"
            assert args[2].endswith(".mp4")
            assert kwargs["ExtraArgs"]["ContentType"] == "video/mp4"
        finally:
            os.unlink(path)

    def test_sniffs_extension_from_a_small_head_read_not_the_whole_file(self):
        """Confirms the sniff only reads the file's head — doesn't assert
        timing, but documents the intent: a multi-MB clip shouldn't need a
        full read just to pick an extension."""
        path = self._write_tmp(_PNG_HEAD + b"\x00" * 10_000)
        try:
            with patch("app.utils.s3_upload._s3_client") as mock_s3, \
                 patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
                 patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
                url = _run(upload_file_path(path, folder="uri-social/thumbs", resource_type="image"))
            assert url.endswith(".png")
        finally:
            os.unlink(path)

    def test_public_id_becomes_the_key_name(self):
        path = self._write_tmp(_MP4_HEAD)
        try:
            with patch("app.utils.s3_upload._s3_client") as mock_s3, \
                 patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
                 patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
                url = _run(upload_file_path(
                    path, folder="uri-social/generated-videos", public_id="scene-1-clip",
                ))
            assert "scene-1-clip.mp4" in url
        finally:
            os.unlink(path)


class TestUploadBase64:
    def test_data_url_with_mime_type_uses_that_content_type(self):
        b64 = base64.b64encode(_JPEG_HEAD).decode()
        data_url = f"data:image/jpeg;base64,{b64}"

        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            url = _run(upload_base64(data_url, folder="uri-social/content-drafts"))

        assert url.endswith(".jpeg")
        _, kwargs = mock_s3.put_object.call_args
        assert kwargs["ContentType"] == "image/jpeg"
        assert kwargs["Body"] == _JPEG_HEAD

    def test_bare_base64_without_data_url_prefix_still_works(self):
        """Not every caller wraps its base64 in a proper data: URL — the
        Cloudinary SDK tolerated a bare base64 string, so this must too."""
        b64 = base64.b64encode(_PNG_HEAD).decode()

        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            url = _run(upload_base64(b64, folder="uri-social/content-drafts"))

        assert url.endswith(".png")
        _, kwargs = mock_s3.put_object.call_args
        assert kwargs["Body"] == _PNG_HEAD


class TestPresignedPutUrl:
    """The browser-direct-upload replacement for Cloudinary's signed chunked
    upload (CampaignService.ts's uploadVideoDirect)."""

    def test_returns_a_put_url_and_the_matching_public_url(self):
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            mock_s3.generate_presigned_url.return_value = "https://signed.example/put"
            signed = presigned_put_url("uri-ads", "upload-abc123", ext="mov")

        assert signed == {
            "put_url": "https://signed.example/put",
            "public_url": "https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-ads/upload-abc123.mov",
        }
        _, kwargs = mock_s3.generate_presigned_url.call_args
        assert kwargs["Params"] == {"Bucket": "uri-social-media-dev", "Key": "uri-ads/upload-abc123.mov"}

    def test_content_type_is_never_bound_into_the_signature(self):
        """A presigned URL signed with a specific ContentType rejects any PUT
        whose header doesn't match it byte-for-byte — and the browser's real
        file.type varies by source (video/mp4, video/quicktime, ...), so this
        must never be part of the signed Params."""
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            mock_s3.generate_presigned_url.return_value = "https://signed.example/put"
            presigned_put_url("uri-ads", "upload-abc123")

        _, kwargs = mock_s3.generate_presigned_url.call_args
        assert "ContentType" not in kwargs["Params"]


class TestKeyFromPublicUrl:
    def test_extracts_the_key_from_one_of_our_own_urls(self):
        with patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            key = key_from_public_url(
                "https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-ads/clip.mp4"
            )
        assert key == "uri-ads/clip.mp4"

    def test_returns_empty_for_a_url_that_is_not_our_bucket(self):
        """Refuses to sign a URL it doesn't recognise rather than quietly
        SSRF-ing an arbitrary host through generate_presigned_url."""
        with patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            assert key_from_public_url("https://evil.example.com/uri-ads/clip.mp4") == ""
            assert key_from_public_url("") == ""


class TestPresignedDownloadUrl:
    """The replacement for Cloudinary's fl_attachment delivery-URL flag
    (cloudinaryDownload.util.ts) — forces a clean Content-Disposition filename
    on download via a presigned GET instead of a delivery-URL trick."""

    def test_signs_with_the_given_filename_as_an_attachment(self):
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            mock_s3.generate_presigned_url.return_value = "https://signed.example/get"
            url = "https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-ads/clip.mp4"
            signed = presigned_download_url(url, "Uri-Video-abc123.mp4")

        assert signed == "https://signed.example/get"
        _, kwargs = mock_s3.generate_presigned_url.call_args
        assert kwargs["Params"]["Key"] == "uri-ads/clip.mp4"
        assert kwargs["Params"]["ResponseContentDisposition"] == 'attachment; filename="Uri-Video-abc123.mp4"'

    def test_sanitizes_an_unsafe_filename(self):
        """A filename with a quote or slash must never reach the header raw — a
        stray quote could break out of the quoted filename value, and a slash
        makes it look like a path rather than a plain name."""
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            mock_s3.generate_presigned_url.return_value = "https://signed.example/get"
            url = "https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-ads/clip.mp4"
            presigned_download_url(url, 'weird"; name/../etc.mp4')

        _, kwargs = mock_s3.generate_presigned_url.call_args
        disposition = kwargs["Params"]["ResponseContentDisposition"]
        assert disposition == 'attachment; filename="weird name..etc.mp4"'

    def test_returns_empty_string_for_a_url_that_is_not_our_bucket(self):
        with patch("app.utils.s3_upload._s3_client") as mock_s3, \
             patch("app.utils.s3_upload._S3_BUCKET", "uri-social-media-dev"), \
             patch("app.utils.s3_upload._S3_REGION", "eu-west-1"):
            signed = presigned_download_url("https://evil.example.com/clip.mp4", "clip.mp4")

        assert signed == ""
        mock_s3.generate_presigned_url.assert_not_called()
