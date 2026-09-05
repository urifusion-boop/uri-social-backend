"""
skin_tone_check.py (VSG-01 v3 §1.7) — verify_skin_rendering()'s response
parsing/normalisation and assert_skin_rendering_compliant()'s gate.

Mocks only the OpenAI client boundary (app.services.AIService.client),
matching the established pattern in tests/test_10_whatsapp_media.py for
GPT-vision services in this codebase — this is the standard way to
unit-test a third-party API integration deterministically, not a stand-in
for verifying the feature itself.

Both paths were also confirmed live, separately from these mocked unit
tests: the success path against the real deployed dev credential (AWS SSM
/uri/social-backend/dev/OPENAI_API_KEY) across three real stock photos —
see skin_tone_check.py's own module docstring for the exact results — and
the fail-safe path against this repo's local .env key, which turned out
to be a stale, unrelated value that doesn't actually authenticate (a real
401 from OpenAI directly, not a config-loading bug); that accident
exercised the except-block for real before the real SSM key was found and
checked, returning the same fail-safe shape asserted here.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.agents.jane_ads.skin_tone_check import (
    SkinToneMismatch,
    verify_skin_rendering,
    assert_skin_rendering_compliant,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_response(content: str):
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


class TestVerifySkinRendering:
    def test_compliant_result_parsed_correctly(self):
        content = (
            '{"contains_person": true, "skin_tone_observed": "deep brown", '
            '"matches_target_range": true, "confidence": "high", "notes": "matches target range"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result == {
            "contains_person": True,
            "skin_tone_observed": "deep brown",
            "matches_target_range": True,
            "confidence": "high",
            "notes": "matches target range",
        }

    def test_no_person_case(self):
        content = (
            '{"contains_person": false, "skin_tone_observed": null, '
            '"matches_target_range": true, "confidence": "high", "notes": "no person visible"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result["contains_person"] is False
        assert result["matches_target_range"] is True

    def test_mismatch_case(self):
        content = (
            '{"contains_person": true, "skin_tone_observed": "fair/light", '
            '"matches_target_range": false, "confidence": "high", '
            '"notes": "rendered person has fair skin, not the target range"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result["contains_person"] is True
        assert result["matches_target_range"] is False

    def test_markdown_fenced_json_is_still_parsed(self):
        content = (
            '```json\n{"contains_person": true, "skin_tone_observed": "medium brown", '
            '"matches_target_range": true, "confidence": "medium", "notes": "close enough"}\n```'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result["skin_tone_observed"] == "medium brown"

    def test_literal_null_string_quirk_is_normalised_to_none(self):
        """The exact GPT-4o-mini vision quirk previously diagnosed in
        ProductAnalysisService's own bug history: a literal "null" string
        instead of a real JSON null."""
        content = (
            '{"contains_person": false, "skin_tone_observed": "null", '
            '"matches_target_range": true, "confidence": "high", "notes": "no person"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result["skin_tone_observed"] is None

    def test_api_failure_returns_a_fail_safe_noncompliant_result(self):
        """Confirmed against a genuine live failure during development
        (see module docstring) — this test reproduces the same contract
        deterministically."""
        bad_client = MagicMock()
        bad_client.chat.completions.create.side_effect = Exception("API unavailable")
        with patch("app.services.AIService.client", bad_client):
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result["contains_person"] is True
        assert result["matches_target_range"] is False

    def test_malformed_json_returns_a_fail_safe_noncompliant_result(self):
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response("not json at all")
            result = _run(verify_skin_rendering("https://example.com/img.png"))
        assert result["contains_person"] is True
        assert result["matches_target_range"] is False


class TestAssertSkinRenderingCompliant:
    def test_raises_on_mismatch(self):
        content = (
            '{"contains_person": true, "skin_tone_observed": "fair/light", '
            '"matches_target_range": false, "confidence": "high", "notes": "too light"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            with pytest.raises(SkinToneMismatch):
                _run(assert_skin_rendering_compliant("https://example.com/img.png"))

    def test_does_not_raise_when_compliant(self):
        content = (
            '{"contains_person": true, "skin_tone_observed": "deep brown", '
            '"matches_target_range": true, "confidence": "high", "notes": "matches"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(assert_skin_rendering_compliant("https://example.com/img.png"))
        assert result["matches_target_range"] is True

    def test_does_not_raise_when_no_person_present(self):
        content = (
            '{"contains_person": false, "skin_tone_observed": null, '
            '"matches_target_range": true, "confidence": "high", "notes": "no person"}'
        )
        with patch("app.services.AIService.client") as mock_client:
            mock_client.chat.completions.create.return_value = _fake_response(content)
            result = _run(assert_skin_rendering_compliant("https://example.com/img.png"))
        assert result["contains_person"] is False

    def test_a_verification_call_failure_also_raises(self):
        """The fail-safe result from a verification-call failure has
        contains_person=True/matches_target_range=False — this must also
        trip the gate, not silently pass an unverifiable image through."""
        bad_client = MagicMock()
        bad_client.chat.completions.create.side_effect = Exception("API unavailable")
        with patch("app.services.AIService.client", bad_client):
            with pytest.raises(SkinToneMismatch):
                _run(assert_skin_rendering_compliant("https://example.com/img.png"))
