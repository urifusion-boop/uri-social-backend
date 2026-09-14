"""
Text on a Face (VSG-01 v3 §2.7) — the highest-policy-risk format in the
library: three independent hard-check guards (permission, viewer
presumption, disallowed personal topics) plus the one-line-only
composition rule, and the false-positive fix found while building this
(generic first-person distress language must not trip the personal-topic
guard the way a second-person presumption should).
"""
import pytest

from app.agents.jane_ads.ad_formats.text_on_a_face import (
    FORMAT,
    PermissionNotOnFile,
    ViewerPresumption,
    DisallowedPersonalTopic,
    TextNotOneLine,
    build_document,
    _check_one_line,
)
from app.agents.jane_ads.ad_formats.legibility import check_legibility
from app.agents.jane_ads.ad_formats.tokens import PLACEHOLDER_TOKENS

PHOTO_URL = "https://example.com/portrait.png"


class TestFormatDefinition:
    def test_upload_asset_source_requires_real_customer_photo(self):
        assert FORMAT.asset_source == "upload"
        assert FORMAT.requires == ["real_customer_photo"]
        assert FORMAT.layers_used == "L4"


class TestPermissionGuard:
    def test_false_is_rejected(self):
        with pytest.raises(PermissionNotOnFile):
            build_document(PHOTO_URL, "I fix every phone myself", permission_on_file=False)

    def test_has_no_default_value(self):
        import inspect
        param = inspect.signature(build_document).parameters["permission_on_file"]
        assert param.default is inspect.Parameter.empty


class TestViewerPresumptionGuard:
    @pytest.mark.parametrize("statement", [
        "Are you struggling with slow deliveries?",
        "Do you feel like nothing works out?",
        "Have you been dealing with unreliable suppliers?",
        "Are you looking for a better tailor?",
    ])
    def test_named_pattern_rejected(self, statement):
        with pytest.raises((ViewerPresumption, DisallowedPersonalTopic)):
            build_document(PHOTO_URL, statement, True)

    @pytest.mark.parametrize("statement", [
        "I fix every phone myself, same day",
        "Every stitch is done by hand in my shop",
        "I answer every call personally",
    ])
    def test_seller_statement_of_position_is_allowed(self, statement):
        doc = build_document(PHOTO_URL, statement, True)
        assert doc is not None


class TestDisallowedPersonalTopicGuard:
    @pytest.mark.parametrize("statement", [
        "I know what weight loss feels like",
        "Everyone deserves to escape debt",
        "I understand loneliness better than most",
    ])
    def test_named_topics_rejected(self, statement):
        with pytest.raises(DisallowedPersonalTopic):
            build_document(PHOTO_URL, statement, True)

    def test_first_person_struggle_is_not_a_topic_violation(self):
        """The false positive found while building this: 'struggle' isn't
        itself a health/body/finance/personal-circumstance topic — it only
        matters when aimed at the viewer (ViewerPresumption's job), not as
        a standalone banned word. A seller's own first-person 'observed
        situation' framing is exactly what §2.7 permits."""
        doc = build_document(PHOTO_URL, "I struggled for years to find reliable suppliers", True)
        assert doc is not None

    def test_relationship_word_alone_is_not_a_topic_violation(self):
        """Same class of false positive: 'relationships with clients' is
        ordinary business language, not personal circumstance."""
        doc = build_document(PHOTO_URL, "I value long-term relationships with every client", True)
        assert doc is not None


class TestOneLineGuard:
    def test_more_than_one_line_is_rejected(self):
        """Tests the guard directly with manufactured lines rather than
        routing through wrap_text: how many lines a string wraps to
        depends on the real render font's metrics, which this machine's
        font-path fallback can't reproduce exactly (see _text_metrics.py's
        own docstring)."""
        with pytest.raises(TextNotOneLine):
            _check_one_line(["first line", "second line"])

    def test_exactly_one_line_is_accepted(self):
        _check_one_line(["a single short line"])  # does not raise

    def test_short_statement_is_accepted(self):
        doc = build_document(PHOTO_URL, "I fix every phone myself, same day", True)
        assert doc is not None


class TestBuildDocument:
    def _doc(self, statement="I fix every phone myself, same day"):
        return build_document(PHOTO_URL, statement, True)

    def test_photo_fills_the_whole_canvas(self):
        doc = self._doc()
        photo = next(l for l in doc["layers"] if l["type"] == "composited_product")
        assert photo["width"] == doc["canvas"]["width"]
        assert photo["height"] == doc["canvas"]["height"]

    def test_plate_sits_at_lower_mid_face(self):
        doc = self._doc()
        height = doc["canvas"]["height"]
        plate = next(l for l in doc["layers"] if l["type"] == "shape" and l.get("fill_color"))
        assert 0.45 * height < plate["y"] < 0.65 * height

    def test_text_is_never_drawn_without_the_plate_beneath_it(self):
        """§2.7: 'Text over skin tones is where legibility fails first —
        solid plate or hard outline only.' Enforced by construction: this
        module has exactly one text layer and one plate shape, always
        both present together."""
        doc = self._doc()
        assert sum(1 for l in doc["layers"] if l["type"] == "text") == 1
        assert sum(1 for l in doc["layers"] if l["type"] == "shape") == 1

    def test_text_is_centred_on_the_plate(self):
        doc = self._doc()
        text = next(l for l in doc["layers"] if l["type"] == "text")
        assert text["text_align"] == "ma"
        assert text["x"] == doc["canvas"]["width"] // 2

    def test_passes_its_own_legibility_self_check(self):
        doc = self._doc()
        assert check_legibility(doc, PLACEHOLDER_TOKENS) == []
