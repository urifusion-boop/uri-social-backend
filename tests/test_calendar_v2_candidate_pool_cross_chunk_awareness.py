"""
Content Calendar V2 — candidate-generation chunks were fully blind to each
other.

_generate_candidate_concepts builds its 80-concept pool as N concurrent
chunks (CANDIDATE_CHUNK_SIZE=20 each), each one an independent LLM call with
no visibility into what any OTHER chunk is proposing. For a business with a
genuinely narrow content space, this let different chunks converge on the
same territory/subject/angle combination — confirmed live: a 30-day plan
still landed at 25/30 items flagged "similar" even after the diversity-check
bugs were fixed, because the underlying CANDIDATE pool itself had real,
substantial duplication the selection step's exact-match overlap check
couldn't fully catch (free-text subject wording varies even for the same
underlying idea).

Fix: split chunk generation into two sequential waves. Wave 1 runs exactly
as before (concurrent, blind). Wave 2 gets wave 1's full output as an
explicit "already proposed in this batch, don't duplicate" block — the
same avoid-block mechanism already used for a PRIOR plan's concepts
(creative_memory), just applied within THIS generation run too.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc


def _concept(territory, subject, angle, concept_name):
    return {
        "territory": territory, "subject": subject, "angle": angle,
        "creative_device": {"category": "visual", "device": "Close-up"},
        "format_hint": "image", "objective": "engagement",
        "audience_segment": "general", "concept_name": concept_name,
    }


def test_concept_pool_avoid_block_formats_territory_subject_angle():
    concepts = [_concept("A_PROBLEM", "shipping delays", "mythbusting", "The Delay Myth")]
    block = svc._concept_pool_avoid_block(concepts)
    assert "territory=A_PROBLEM" in block
    assert "subject=shipping delays" in block
    assert "angle=mythbusting" in block
    assert "The Delay Myth" in block
    assert "Already proposed earlier in this SAME batch" in block


def test_concept_pool_avoid_block_empty_for_no_concepts():
    assert svc._concept_pool_avoid_block([]) == ""


def test_wave_two_prompts_include_wave_ones_concepts():
    """target_count=40 with CANDIDATE_CHUNK_SIZE=20 produces exactly 2
    chunks — one per wave — so wave 2's single chunk prompt must carry
    wave 1's single chunk's concepts as an avoid block."""
    captured_prompts = []

    async def fake(req):
        prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
        captured_prompts.append(prompt)
        if len(captured_prompts) == 1:
            # Wave 1's chunk returns one concept with a distinctive signature.
            payload = [{
                "territory": "A_PROBLEM", "subject": "shipping delays", "angle": "mythbusting",
                "creative_device": {"category": "visual", "device": "Close-up"},
                "format_hint": "image", "objective": "engagement",
                "audience_segment": "first-time buyers", "concept_name": "The Delay Myth",
            }]
        else:
            payload = [{
                "territory": "B_PRIDE", "subject": "fast delivery", "angle": "celebration",
                "creative_device": {"category": "visual", "device": "Close-up"},
                "format_hint": "image", "objective": "engagement",
                "audience_segment": "repeat buyers", "concept_name": "The Speed Win",
            }]
        import json
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(payload)
        ))])

    with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
         patch.object(svc.AIService, "chat_completion", side_effect=fake):
        result = asyncio.run(svc._generate_candidate_concepts(
            brand={"brand_name": "Acme"}, framework=svc.get_creative_framework(""),
            existing_assets_summary="", creative_memory={}, platforms=["instagram"],
            target_count=40,
        ))

    assert len(captured_prompts) == 2
    wave1_prompt, wave2_prompt = captured_prompts

    # Wave 1 had nothing to avoid from within this run.
    assert "Already proposed earlier in this SAME batch" not in wave1_prompt
    # Wave 2 must see wave 1's actual concept.
    assert "Already proposed earlier in this SAME batch" in wave2_prompt
    assert "territory=A_PROBLEM" in wave2_prompt
    assert "The Delay Myth" in wave2_prompt

    assert len(result) == 2
    assert {c["concept_name"] for c in result} == {"The Delay Myth", "The Speed Win"}


def test_single_wave_when_pool_fits_in_one_chunk():
    """target_count <= CANDIDATE_CHUNK_SIZE should produce one chunk and
    skip wave 2 entirely — no behavior change for the small-pool case."""
    captured_prompts = []

    async def fake(req):
        prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
        captured_prompts.append(prompt)
        import json
        payload = [{
            "territory": "A_PROBLEM", "subject": "s", "angle": "a",
            "creative_device": {"category": "visual", "device": "d"},
            "format_hint": "image", "objective": "engagement",
            "audience_segment": "x", "concept_name": "c",
        } for _ in range(10)]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(payload)
        ))])

    with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
         patch.object(svc.AIService, "chat_completion", side_effect=fake):
        result = asyncio.run(svc._generate_candidate_concepts(
            brand={}, framework=svc.get_creative_framework(""), existing_assets_summary="",
            creative_memory={}, platforms=["instagram"], target_count=10,
        ))

    assert len(captured_prompts) == 1
    assert len(result) == 10
