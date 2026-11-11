"""
Unit tests for the Cloudinary->S3 backfill script's pure logic — the
recursive URL finder and the key-extraction regex. No real Mongo/network
calls here (run() itself is exercised manually against a real environment,
not under test); this just locks down the two pieces most likely to silently
miss or mis-migrate a real document field.
"""
from scripts.migrate_cloudinary_to_s3 import (
    CLOUDINARY_URL_RE,
    _find_cloudinary_urls,
    _key_from_cloudinary_url,
)


def test_matches_real_cloudinary_delivery_url():
    assert CLOUDINARY_URL_RE.match(
        "https://res.cloudinary.com/df8ckaeam/image/upload/v123/uri-social/logos/x.png"
    )
    assert CLOUDINARY_URL_RE.match(
        "https://res.cloudinary.com/df8ckaeam/video/upload/uri-video-production/x.mp4"
    )
    assert CLOUDINARY_URL_RE.match(
        "https://res.cloudinary.com/df8ckaeam/raw/upload/uri-music/custom/x.mp3"
    )


def test_does_not_match_already_migrated_s3_url():
    assert not CLOUDINARY_URL_RE.match(
        "https://uri-social-media-prod.s3.eu-west-1.amazonaws.com/uri-social/logos/x.png"
    )


def test_does_not_match_unrelated_string():
    assert not CLOUDINARY_URL_RE.match("not a url at all")
    assert not CLOUDINARY_URL_RE.match("https://example.com/image.png")


def test_key_from_cloudinary_url_strips_version_and_cloud_prefix():
    url = "https://res.cloudinary.com/df8ckaeam/image/upload/v1234567890/uri-social/logos/abc.png"
    assert _key_from_cloudinary_url(url) == "uri-social/logos/abc.png"


def test_key_from_cloudinary_url_without_version_segment():
    url = "https://res.cloudinary.com/df8ckaeam/raw/upload/uri-music/custom/track.mp3"
    assert _key_from_cloudinary_url(url) == "uri-music/custom/track.mp3"


def test_find_cloudinary_urls_top_level_field():
    doc = {"_id": "x", "logo_url": "https://res.cloudinary.com/c/image/upload/a.png"}
    hits = list(_find_cloudinary_urls(doc))
    assert hits == [("logo_url", "https://res.cloudinary.com/c/image/upload/a.png")]


def test_find_cloudinary_urls_skips_id_field():
    """_id is never a media URL and its ObjectId isn't even a string — must
    be skipped explicitly rather than relying on the isinstance(str) check
    alone to avoid walking into it."""
    doc = {"_id": "not-a-real-objectid-but-should-still-be-skipped"}
    assert list(_find_cloudinary_urls(doc)) == []


def test_find_cloudinary_urls_nested_in_list_of_dicts():
    """Mirrors brand_profiles.primary_custom_fonts[].url — a Cloudinary URL
    nested inside a list of dicts, at a field name with no special handling."""
    doc = {
        "primary_custom_fonts": [
            {"name": "Font A", "url": "https://res.cloudinary.com/c/raw/upload/fonts/a.ttf"},
            {"name": "Font B", "url": "https://uri-social-media-prod.s3.eu-west-1.amazonaws.com/fonts/b.ttf"},
        ]
    }
    hits = list(_find_cloudinary_urls(doc))
    assert hits == [("primary_custom_fonts.0.url", "https://res.cloudinary.com/c/raw/upload/fonts/a.ttf")]


def test_find_cloudinary_urls_deeply_nested_mixed_structure():
    doc = {
        "slides": [
            {"image_url": "https://res.cloudinary.com/c/image/upload/slide0.png"},
            {"image_url": "https://res.cloudinary.com/c/image/upload/slide1.png", "meta": {"thumb": None}},
        ],
        "unrelated_flag": True,
        "unrelated_count": 3,
    }
    hits = list(_find_cloudinary_urls(doc))
    paths = {p for p, _ in hits}
    assert paths == {"slides.0.image_url", "slides.1.image_url"}


def test_find_cloudinary_urls_returns_nothing_for_fully_migrated_document():
    doc = {
        "_id": "x",
        "image_url": "https://uri-social-media-prod.s3.eu-west-1.amazonaws.com/uri-social/x.png",
        "nested": {"url": "https://uri-social-media-prod.s3.eu-west-1.amazonaws.com/uri-social/y.png"},
    }
    assert list(_find_cloudinary_urls(doc)) == []
