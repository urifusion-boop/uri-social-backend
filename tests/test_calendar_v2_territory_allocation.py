"""
Even after making candidate-generation chunks aware of each other's output
(the wave-splitting fix, see test_calendar_v2_candidate_pool_cross_chunk_awareness.py),
a real regenerate on URI's live calendar stayed at ~25-26/30 items flagged
"similar" — essentially no improvement. The cross-chunk awareness fix only
discourages near-identical restatements; it never required the model to
actually use the other 8-9 unused content territories out of the 11
available (app/agents/content_calendar_v2/creative_framework.py's
TERRITORIES — Problem, Desire, Curiosity, Contrarian, Proof, People,
Process, Comparison, Education, Culture/Context, Entertainment, Commercial).
The old prompt line said "draw from these freely — you don't need every
one," which let the model default to whichever 2-3 territories felt safest
for the brand every single time, regardless of how much unused breadth
existed.

_allocate_territories fixes the actual root cause: deterministically assign
which territories each generation chunk must draw from (round-robin,
respecting any territory's own max_share cap — e.g. L_COMMERCIAL's PRD §8
"must not dominate" ceiling), instead of leaving the choice to the model.
Every concept from any territory is still fully grounded in the specific
brand (name/audience/voice/rules are injected into every chunk's prompt
unconditionally, untouched by this change) — a territory is a framing lens
(problem vs. proof vs. process, etc.), never a different business or topic.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc


def _framework_with_territories(territory_specs):
    """territory_specs: dict of key -> optional max_share."""
    territories = {
        key: {
            "label": key.split("_", 1)[-1].title(),
            "description": f"Explore {key}.",
            "subjects": ["Subject A", "Subject B"],
            **({"max_share": max_share} if max_share is not None else {}),
        }
        for key, max_share in territory_specs.items()
    }
    fw = svc.get_creative_framework("")
    fw = {**fw, "territories": territories}
    return fw


class TestAllocateTerritories:
    def test_distributes_evenly_round_robin(self):
        territories = {"A": {"label": "A"}, "B": {"label": "B"}, "C": {"label": "C"}}
        result = svc._allocate_territories(territories, 9)
        assert len(result) == 9
        counts = {k: result.count(k) for k in territories}
        assert counts == {"A": 3, "B": 3, "C": 3}

    def test_interleaved_not_grouped(self):
        territories = {"A": {"label": "A"}, "B": {"label": "B"}}
        result = svc._allocate_territories(territories, 4)
        # Round-robin means A and B alternate, not AABB.
        assert result == ["A", "B", "A", "B"]

    def test_touches_every_territory_for_a_realistic_pool_size(self):
        """The actual bug: a model free to choose only ever used 2-3 of 11
        territories. With 80 candidates across 11 territories, every single
        one must get at least a handful of slots."""
        territories = {f"T{i}": {"label": f"T{i}"} for i in range(11)}
        result = svc._allocate_territories(territories, 80)
        counts = {k: result.count(k) for k in territories}
        assert all(c > 0 for c in counts.values())
        assert min(counts.values()) >= 7  # 80 // 11 == 7

    def test_max_share_cap_is_respected(self):
        territories = {
            "A": {"label": "A"}, "B": {"label": "B"}, "C": {"label": "C"},
            "COMMERCIAL": {"label": "Commercial", "max_share": 0.20},
        }
        result = svc._allocate_territories(territories, 100)
        assert result.count("COMMERCIAL") <= 20

    def test_empty_territories_returns_empty(self):
        assert svc._allocate_territories({}, 50) == []

    def test_zero_total_returns_empty(self):
        territories = {"A": {"label": "A"}}
        assert svc._allocate_territories(territories, 0) == []


class TestTerritoryAllocationWiredIntoPrompt:
    def test_each_chunk_prompt_carries_its_required_territory_mix(self):
        captured_prompts = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            captured_prompts.append(prompt)
            payload = [{
                "territory": "A", "subject": "s", "angle": "a",
                "creative_device": {"category": "visual", "device": "d"},
                "format_hint": "image", "objective": "engagement",
                "audience_segment": "x", "concept_name": f"c{len(captured_prompts)}",
            } for _ in range(20)]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(payload)
            ))])

        fw = _framework_with_territories({
            "A_PROBLEM": None, "B_DESIRE": None, "C_PROOF": None, "D_PEOPLE": None,
        })

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_candidate_concepts(
                brand={"brand_name": "URI"}, framework=fw,
                existing_assets_summary="", creative_memory={}, platforms=["instagram"],
                target_count=80,
            ))

        assert len(captured_prompts) == 4  # 80 / CANDIDATE_CHUNK_SIZE(20)
        for prompt in captured_prompts:
            assert "THIS BATCH'S REQUIRED TERRITORY MIX" in prompt
            assert "entirely\nabout URI specifically" in prompt or "entirely about URI specifically" in prompt
            assert "draw from these freely" not in prompt

    def test_commercial_cap_reflected_in_a_real_run(self):
        """With a 20% cap and 80 total, COMMERCIAL should never be asked for
        more than 16 concepts across the whole run."""
        captured_prompts = []

        async def fake(req):
            prompt = req["messages"][0]["content"] if isinstance(req, dict) else ""
            captured_prompts.append(prompt)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps([])
            ))])

        fw = _framework_with_territories({
            "A_PROBLEM": None, "B_DESIRE": None, "L_COMMERCIAL": 0.20,
        })

        with patch.object(svc.AIService, "build_ai_model", side_effect=lambda **kw: kw), \
             patch.object(svc.AIService, "chat_completion", side_effect=fake):
            asyncio.run(svc._generate_candidate_concepts(
                brand={"brand_name": "URI"}, framework=fw,
                existing_assets_summary="", creative_memory={}, platforms=["instagram"],
                target_count=80,
            ))

        import re
        total_commercial = 0
        for prompt in captured_prompts:
            m = re.search(r"(\d+) concept\(s\) from L_COMMERCIAL", prompt)
            if m:
                total_commercial += int(m.group(1))
        assert total_commercial <= 16
