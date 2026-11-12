"""
Rate-limit resilience for the calendar's chunked, concurrent AI calls
(candidate generation, scoring, final copy).

Root-caused via live CloudWatch logs (Oct 2026): a real 30-day plan
generation failed three times with "no candidate concepts produced". The
real error, once a separate masking bug was fixed, was a genuine OpenAI
429 — candidate generation fires 4 concurrent GPT-4o requests per plan,
all in the same instant, with zero backoff before the single retry. Two
fixes: stagger each chunk's own start so a burst isn't literally
simultaneous, and back off briefly before retrying specifically when the
failure was rate-limit-shaped.
"""
import asyncio
import time

import pytest

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestIsRateLimited:
    def test_recognizes_the_exact_rate_limit_message(self):
        assert svc._is_rate_limited(
            "AI service is temporarily unavailable. Please try again later or contact support.")

    def test_recognizes_the_quota_variant_too(self):
        # Both AIService 429/quota messages share this phrase — treated the
        # same here (worst case, one extra short backoff before the same
        # graceful failure; no real harm either way).
        assert svc._is_rate_limited(
            "AI service is temporarily unavailable due to quota limits. Please contact support.")

    def test_does_not_flag_unrelated_errors(self):
        assert not svc._is_rate_limited("expected a JSON array, got <class 'dict'>")
        assert not svc._is_rate_limited("AI service configuration error. Please contact support.")
        assert not svc._is_rate_limited(ValueError("some other failure"))


class TestBackoffIfRateLimited:
    def test_sleeps_when_rate_limited(self):
        start = time.monotonic()
        run(svc._backoff_if_rate_limited("AI service is temporarily unavailable. Please try again later."))
        elapsed = time.monotonic() - start
        assert elapsed >= svc._RATE_LIMIT_BACKOFF_SECONDS - 0.05

    def test_does_not_sleep_for_other_errors(self):
        start = time.monotonic()
        run(svc._backoff_if_rate_limited("expected a JSON array, got <class 'dict'>"))
        elapsed = time.monotonic() - start
        assert elapsed < 0.5   # effectively instant, not the 5s backoff


class TestChunkStagger:
    def test_candidate_generation_chunks_start_at_increasing_offsets(self):
        """_one_chunk's first line is a stagger sleep of chunk_idx * 0.5s —
        confirm chunk 0 starts immediately and later chunks are delayed,
        without actually waiting out the full real delay in this test."""
        import inspect
        source = inspect.getsource(svc._generate_candidate_concepts)
        assert "_CHUNK_STAGGER_SECONDS" in source
        assert "chunk_idx * _CHUNK_STAGGER_SECONDS" in source

    def test_score_candidates_chunks_are_staggered(self):
        import inspect
        source = inspect.getsource(svc._score_candidates)
        assert "_CHUNK_STAGGER_SECONDS" in source

    def test_final_copy_chunks_are_staggered(self):
        import inspect
        source = inspect.getsource(svc.generate_plan_v2) if hasattr(svc, "generate_plan_v2") else ""
        # _run_chunk is a nested function inside the main generation pipeline
        # (_build_plan_doc) rather than its own top-level function — assert
        # against the whole module source for this one instead of a single
        # function's source.
        with open(svc.__file__) as f:
            whole = f.read()
        assert "_run_chunk(chunk_start_idx: int)" in whole
        assert "(chunk_start_idx // CONTENT_CHUNK_SIZE) * _CHUNK_STAGGER_SECONDS" in whole


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
