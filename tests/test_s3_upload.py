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
from unittest.mock import MagicMock, patch

from app.utils.s3_upload import _sniff_extension, upload_base64, upload_bytes

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
