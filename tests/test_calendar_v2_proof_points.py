"""
Root cause found by pulling URI's real flagged items and its real stored
Brand Playbook data: 7 of 10 customer-psychology fields were completely
empty, the 2 non-empty ones were corrupted by a frontend comma-split bug
(fixed separately in uri-social-frontend), and there was no field anywhere
for real proof (testimonials/case studies/results) — so "Proof"-territory
posts had nothing true to cite and invented vague, generic-sounding
"customer success" filler instead, correctly caught by the diversity
checker's GENERIC criterion.

proof_points is a new Brand Playbook field (real quotes/results only) wired
into _customer_psychology_block: when present, the model is told to quote/
cite them close to verbatim; when absent, it's explicitly told NOT to
fabricate a testimonial/case study/result, so a thin Playbook produces
honest content instead of confident-sounding filler.
"""
from app.agents.content_calendar_v2.services.content_calendar_v2_service import _customer_psychology_block


class TestProofPointsBlock:
    def test_real_proof_points_are_included_verbatim_instruction(self):
        brand = {
            "customer_pain_points": ["no time to cook"],
            "proof_points": ["\"URI cut our content time in half\" — Jane, Acme Co.", "40% engagement increase in 2 months"],
        }
        block = _customer_psychology_block(brand)
        assert "REAL PROOF AVAILABLE" in block
        assert "URI cut our content time in half" in block
        assert "40% engagement increase in 2 months" in block
        assert "NO REAL PROOF POINTS" not in block

    def test_missing_proof_points_triggers_anti_fabrication_instruction(self):
        brand = {"customer_pain_points": ["no time to cook"]}
        block = _customer_psychology_block(brand)
        assert "NO REAL PROOF POINTS ARE ON FILE" in block
        assert "Do NOT invent a testimonial" in block
        assert "REAL PROOF AVAILABLE" not in block

    def test_anti_fabrication_instruction_present_even_when_playbook_otherwise_empty(self):
        """A near-blank Playbook (URI's real prior state) must still get the
        anti-fabrication instruction, not silence — the old code returned ''
        entirely whenever `parts` was empty, which meant a thin brand got NO
        warning against inventing proof at all."""
        block = _customer_psychology_block({})
        assert block != ""
        assert "NO REAL PROOF POINTS ARE ON FILE" in block

    def test_empty_proof_points_list_treated_same_as_missing(self):
        brand = {"customer_pain_points": ["x"], "proof_points": []}
        block = _customer_psychology_block(brand)
        assert "NO REAL PROOF POINTS ARE ON FILE" in block

    def test_blank_proof_point_strings_are_filtered_out(self):
        brand = {"customer_pain_points": ["x"], "proof_points": ["", "  ", "A real result"]}
        block = _customer_psychology_block(brand)
        assert "REAL PROOF AVAILABLE" in block
        assert "A real result" in block
