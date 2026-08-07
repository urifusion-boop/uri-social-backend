"""
Regression test for upload_bytes()'s public_id parameter.

Live-confirmed on production: the custom-font-upload endpoint
(complete_social_manager.upload_custom_font) calls upload_bytes(..., public_id=...),
but upload_bytes() never accepted that parameter — every real font upload failed
with "upload_bytes() got an unexpected keyword argument 'public_id'", silently (the
endpoint's broad except still returned 200 via UriResponse's envelope convention, so
nothing surfaced as an HTTP error).
"""
import asyncio
from unittest.mock import patch

from app.utils.cloudinary_upload import upload_bytes


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_upload_bytes_passes_public_id_through_when_given():
    with patch("cloudinary.uploader.upload") as mock_upload:
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/x/raw/upload/font"}
        url = _run(upload_bytes(
            b"fake-font-bytes", folder="uri-social/custom-fonts/u1",
            resource_type="raw", public_id="MTNBRIGHTERSANS-LIGHT",
        ))
    assert url == "https://res.cloudinary.com/x/raw/upload/font"
    _, kwargs = mock_upload.call_args
    assert kwargs["public_id"] == "MTNBRIGHTERSANS-LIGHT"
    assert kwargs["folder"] == "uri-social/custom-fonts/u1"
    assert kwargs["resource_type"] == "raw"


def test_upload_bytes_omits_public_id_when_not_given():
    """Every other existing caller (logos, templates, chat images) doesn't pass
    public_id — confirm the default doesn't send a stray public_id=None to
    Cloudinary's SDK, which would behave differently from omitting it entirely."""
    with patch("cloudinary.uploader.upload") as mock_upload:
        mock_upload.return_value = {"secure_url": "https://res.cloudinary.com/x/image/upload/logo"}
        url = _run(upload_bytes(b"fake-image-bytes", folder="uri-social/logos"))
    assert url == "https://res.cloudinary.com/x/image/upload/logo"
    _, kwargs = mock_upload.call_args
    assert "public_id" not in kwargs
