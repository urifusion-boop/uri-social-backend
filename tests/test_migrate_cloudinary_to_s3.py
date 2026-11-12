"""Covers the pure logic in scripts/migrate_cloudinary_to_s3.py — the
recursive document scanner and the Cloudinary-URL-to-S3-key derivation.
Not the I/O orchestration (real download/upload/DB write), which is a
one-time maintenance script run manually against real data, matching this
repo's existing convention of scripts/ having no pytest coverage for its
top-level run() — but the logic deciding WHAT gets found and WHERE it gets
written deserves real verification before it's ever pointed at live data."""
from scripts.migrate_cloudinary_to_s3 import (
    CLOUDINARY_URL_RE,
    _find_cloudinary_urls,
    _key_from_cloudinary_url,
)

_LOGO_URL = "https://res.cloudinary.com/df8ckaeam/image/upload/v1790322806/uri-social/logos/rfqg9qnr6hhgxjkru4bj.png"
_FONT_URL = "https://res.cloudinary.com/df8ckaeam/raw/upload/v1786109787/uri-social/custom-fonts/u1/Mansfield-iF66c703e50e674"


class TestCloudinaryUrlRegex:
    def test_matches_image_delivery_url(self):
        assert CLOUDINARY_URL_RE.match(_LOGO_URL)

    def test_matches_raw_delivery_url(self):
        assert CLOUDINARY_URL_RE.match(_FONT_URL)

    def test_does_not_match_an_s3_url(self):
        """The idempotency guarantee — a URL already migrated must never
        match again on a re-run."""
        s3_url = "https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/uri-social/logos/x.png"
        assert not CLOUDINARY_URL_RE.match(s3_url)

    def test_does_not_match_an_unrelated_url(self):
        assert not CLOUDINARY_URL_RE.match("https://example.com/image.png")


class TestKeyFromCloudinaryUrl:
    def test_preserves_folder_structure_and_filename(self):
        key = _key_from_cloudinary_url(_LOGO_URL)
        assert key == "uri-social/logos/rfqg9qnr6hhgxjkru4bj.png"

    def test_strips_the_version_segment(self):
        key = _key_from_cloudinary_url(_FONT_URL)
        assert "v1786109787" not in key
        assert key == "uri-social/custom-fonts/u1/Mansfield-iF66c703e50e674"

    def test_handles_url_with_no_version_segment(self):
        url = "https://res.cloudinary.com/df8ckaeam/image/upload/uri-social/logos/abc.png"
        assert _key_from_cloudinary_url(url) == "uri-social/logos/abc.png"


class TestFindCloudinaryUrls:
    def test_finds_top_level_string_field(self):
        doc = {"_id": "x1", "logo_url": _LOGO_URL, "brand_name": "Docerity"}
        hits = list(_find_cloudinary_urls(doc))
        assert hits == [("logo_url", _LOGO_URL)]

    def test_finds_url_nested_inside_a_list_of_dicts(self):
        """The exact shape brand_profiles.primary_custom_fonts is stored
        in — a list of dicts, each with its own url field."""
        doc = {
            "_id": "x1",
            "primary_custom_fonts": [
                {"filename": "a.ttf", "url": "https://cdn.example.com/already-elsewhere.ttf"},
                {"filename": "b.ttf", "url": _FONT_URL},
            ],
        }
        hits = list(_find_cloudinary_urls(doc))
        assert hits == [("primary_custom_fonts.1.url", _FONT_URL)]

    def test_finds_multiple_urls_across_different_fields(self):
        doc = {
            "_id": "x1",
            "logo_url": _LOGO_URL,
            "primary_custom_font_selected_url": _FONT_URL,
        }
        hits = dict(_find_cloudinary_urls(doc))
        assert hits == {"logo_url": _LOGO_URL, "primary_custom_font_selected_url": _FONT_URL}

    def test_skips_the_id_field(self):
        """_id is never a media URL and must never appear as a hit path —
        writing back to it would be nonsensical."""
        doc = {"_id": "x1", "brand_name": "Docerity"}
        hits = list(_find_cloudinary_urls(doc))
        assert hits == []

    def test_ignores_non_cloudinary_urls(self):
        doc = {"_id": "x1", "logo_url": "https://example.com/logo.png"}
        assert list(_find_cloudinary_urls(doc)) == []

    def test_already_migrated_s3_url_is_not_found_again(self):
        doc = {"_id": "x1", "logo_url": "https://uri-social-media-dev.s3.eu-west-1.amazonaws.com/x.png"}
        assert list(_find_cloudinary_urls(doc)) == []

    def test_deeply_nested_dict_inside_list_inside_dict(self):
        doc = {
            "_id": "x1",
            "slides": [
                {"slide_number": 1, "assets": {"image_url": _LOGO_URL}},
            ],
        }
        hits = list(_find_cloudinary_urls(doc))
        assert hits == [("slides.0.assets.image_url", _LOGO_URL)]
