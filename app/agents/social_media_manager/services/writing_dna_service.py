"""
Writing DNA Service
URI Social — Blog Generator Voice Profiler

Generates a persistent writing voice profile from a 16-question quiz,
optional writing sample analysis, and aspirational writer lookup.
The output is a 200-400 word directive prompt injected into every blog generation call.
"""

import json
import re
from datetime import datetime
from typing import Dict, Any, Optional, List
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from openai import AsyncOpenAI

from app.core.config import settings
from app.domain.responses.uri_response import UriResponse

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

COLLECTION = "writing_dna"

# ── Answer → key mappings (A/B/C/D → semantic key) ────────────────────────

OPENING_MAP = {
    "A": "reflective_story",
    "B": "direct_blunt",
    "C": "casual_conversational",
    "D": "provocative_contrarian",
}
STRUCTURE_MAP = {
    "A": "data_driven_precise",
    "B": "short_punchy_confident",
    "C": "flowing_personal_reflective",
    "D": "blunt_opinionated",
}
TEACHING_MAP = {
    "A": "narrative_experiential",
    "B": "structured_logical",
    "C": "analogy_based",
    "D": "myth_busting",
}
JARGON_MAP = {
    "A": "heavy_insider",
    "B": "moderate_with_context",
    "C": "none_accessible",
    "D": "deliberate_translate",
}
HEADLINE_MAP = {
    "A": "vulnerable_story",
    "B": "structured_practical",
    "C": "bold_opinionated",
    "D": "intriguing_understated",
}
HUMOUR_MAP = {
    "A": "none",
    "B": "dry_subtle",
    "C": "natural_witty",
    "D": "bold_playful",
}
CONFRONT_MAP = {
    "A": "diplomatic",
    "B": "blunt",
    "C": "narrative",
    "D": "avoidant",
}
VULN_MAP = {
    "A": "high_open",
    "B": "moderate_purposeful",
    "C": "low_solutions_focused",
    "D": "none_authority",
}
PACING_MAP = {
    "A": "staccato",
    "B": "balanced",
    "C": "flowing",
    "D": "dynamic_varied",
}
CLOSING_MAP = {
    "A": "empowering",
    "B": "prescriptive",
    "C": "vulnerable_honest",
    "D": "community_shareable",
}
PIDGIN_MAP = {
    "A": "none",
    "B": "light",
    "C": "moderate",
    "D": "heavy",
}
REFERENCE_MAP = {
    "A": "nigerian_business",
    "B": "global_tech",
    "C": "personal_local",
    "D": "pop_culture",
}
EDGE_MAP = {
    "A": "clean",
    "B": "mild_nigerian",
    "C": "moderate",
    "D": "bold_raw",
}
ROLE_MAP = {
    "A": "authority",
    "B": "peer",
    "C": "disruptor",
    "D": "guide",
}
ARCHETYPE_MAP = {
    "A": "reflective_philosopher",
    "B": "metrics_driven_achiever",
    "C": "humble_veteran",
    "D": "contrarian_outsider",
}


def _map_answers(answers: Dict[str, str]) -> Dict[str, str]:
    """Map raw A/B/C/D quiz answers to semantic keys."""
    return {
        "opening":       OPENING_MAP.get(answers.get("q1", "B"), "direct_blunt"),
        "structure":     STRUCTURE_MAP.get(answers.get("q2", "C"), "flowing_personal_reflective"),
        "teaching":      TEACHING_MAP.get(answers.get("q3", "A"), "narrative_experiential"),
        "jargon":        JARGON_MAP.get(answers.get("q4", "B"), "moderate_with_context"),
        "headline":      HEADLINE_MAP.get(answers.get("q5", "D"), "intriguing_understated"),
        "humour":        HUMOUR_MAP.get(answers.get("q6", "A"), "none"),
        "confrontation": CONFRONT_MAP.get(answers.get("q7", "A"), "diplomatic"),
        "vulnerability": VULN_MAP.get(answers.get("q8", "B"), "moderate_purposeful"),
        "pacing":        PACING_MAP.get(answers.get("q9", "B"), "balanced"),
        "closing":       CLOSING_MAP.get(answers.get("q10", "B"), "prescriptive"),
        "pidgin":        PIDGIN_MAP.get(answers.get("q11", "A"), "none"),
        "references":    REFERENCE_MAP.get(answers.get("q12", "A"), "nigerian_business"),
        "edge":          EDGE_MAP.get(answers.get("q13", "A"), "clean"),
        "role":          ROLE_MAP.get(answers.get("q14", "B"), "peer"),
        "archetype":     ARCHETYPE_MAP.get(answers.get("q15", "C"), "humble_veteran"),
        "aspirational":  answers.get("q16", ""),
    }


def _build_dna_prompt(dna: Dict[str, str]) -> str:
    """Build the 200-400 word Writing DNA directive from mapped keys."""
    sections = []

    # ARCHETYPE — core identity, leads the prompt
    archetypes = {
        "reflective_philosopher": (
            "You write like someone who thinks deeply about what they do. "
            "You connect business lessons to larger truths about life and work. "
            "You're philosophical but practical. You ask questions that make the reader pause."
        ),
        "metrics_driven_achiever": (
            "You write with numbers and results. You cite specific metrics, timelines, and outcomes. "
            "You're not interested in theory — you care about what worked and what the data says. "
            "Every claim has a number behind it."
        ),
        "humble_veteran": (
            "You write with the authority of experience but the humility of someone who knows they "
            "don't have all the answers. You share what didn't work as openly as what did. "
            "Your honesty is your credibility."
        ),
        "contrarian_outsider": (
            "You write against the grain. You challenge common advice, question popular strategies, "
            "and position your approach as the alternative. You're confident, sometimes provocative, "
            "and always backed by your own results."
        ),
    }
    if arch := archetypes.get(dna.get("archetype", "")):
        sections.append(arch)

    # OPENING
    openings = {
        "reflective_story": (
            "OPENING: Always open with a personal story, an observation, or a moment. "
            "Never with a generic statement. The first paragraph should feel like you're sitting "
            "across from the reader, starting mid-thought."
        ),
        "direct_blunt": (
            "OPENING: Open with the core argument in the first sentence. No preamble. No story. "
            "The reader knows your position before they finish the first paragraph."
        ),
        "casual_conversational": (
            "OPENING: Open like you're continuing a conversation the reader didn't know they were having. "
            "Casual, mid-thought, like a voice note that starts with 'okay so...'"
        ),
        "provocative_contrarian": (
            "OPENING: Open with a challenge or a statement that makes the reader stop scrolling. "
            "Something they disagree with or didn't expect. The opening creates tension."
        ),
    }
    if op := openings.get(dna.get("opening", "")):
        sections.append(op)

    # SENTENCE STRUCTURE
    structures = {
        "data_driven_precise": (
            "SENTENCE STYLE: Include specific numbers, percentages, and metrics throughout. "
            "Not 'revenue grew significantly' but 'revenue grew 40% to N12.8M.' "
            "Every claim should have data behind it."
        ),
        "short_punchy_confident": (
            "SENTENCE STYLE: Short sentences. Fragments are fine. Paragraphs rarely exceed 2 sentences. "
            "The rhythm is staccato: punch, punch, breathe. Confidence comes from brevity, not elaboration."
        ),
        "flowing_personal_reflective": (
            "SENTENCE STYLE: Sentences flow into each other with natural connectors. Longer thoughts "
            "are welcome — 15-25 words per sentence is comfortable. The writing has a spoken quality, "
            "like a podcast transcript of a thoughtful person."
        ),
        "blunt_opinionated": (
            "SENTENCE STYLE: Active voice only. No qualifiers ('perhaps', 'it could be argued'). "
            "State the position directly. 'This approach is wrong' not "
            "'This approach may not be optimal for some businesses.'"
        ),
    }
    if st := structures.get(dna.get("structure", "")):
        sections.append(st)

    # PACING
    pacings = {
        "staccato": (
            "PACING: Short paragraphs, 1-2 sentences each. Lots of white space. "
            "Each thought gets its own breath."
        ),
        "balanced": (
            "PACING: Medium paragraphs, 3-4 sentences. Readable chunks that don't overwhelm."
        ),
        "flowing": (
            "PACING: Longer paragraphs when the thought demands it. Writing flows like speech — "
            "ideas build on each other within the same paragraph."
        ),
        "dynamic_varied": (
            "PACING: Mix it up deliberately. A one-sentence paragraph after a long one. "
            "Rhythm changes keep the reader engaged."
        ),
    }
    if pa := pacings.get(dna.get("pacing", "")):
        sections.append(pa)

    # HUMOUR
    humours = {
        "none": (
            "HUMOUR: No humour. The tone is serious and expert throughout. "
            "Comedy would undermine the authority."
        ),
        "dry_subtle": (
            "HUMOUR: Occasional dry observations that make the reader smirk. "
            "Never a setup-punchline joke. More like a witty aside that rewards attentive readers."
        ),
        "natural_witty": (
            "HUMOUR: Natural humour woven into the writing. You're funny because you see things clearly, "
            "not because you're trying to be funny. The humour is observational."
        ),
        "bold_playful": (
            "HUMOUR: Humour is a core part of the voice. You make the reader laugh. "
            "Not every paragraph, but regularly. The humour makes serious points more memorable."
        ),
    }
    if hu := humours.get(dna.get("humour", "")):
        sections.append(hu)

    # LANGUAGE / PIDGIN
    pidgins = {
        "none": "LANGUAGE: Pure English throughout. No Pidgin, no Nigerian slang, no colloquialisms.",
        "light": (
            "LANGUAGE: Occasional Nigerian expression for emphasis or flavour: "
            "'no wahala', 'e dey work', 'omo'. The base is English but the personality is Nigerian."
        ),
        "moderate": (
            "LANGUAGE: Nigerian English and Pidgin woven naturally into the writing. "
            "Code-switching between formal English and Pidgin is the natural rhythm. "
            "'The metrics are strong, but e no easy reach here o.'"
        ),
        "heavy": (
            "LANGUAGE: Heavy Pidgin throughout. The writing is for Nigerians, by a Nigerian, "
            "in the language Nigerians actually use. "
            "'If you no dey post consistently, your competitors go chop your market finish.'"
        ),
    }
    if pi := pidgins.get(dna.get("pidgin", "")):
        sections.append(pi)

    # VULNERABILITY
    vulnerabilities = {
        "high_open": (
            "VULNERABILITY: Share failures, mistakes, and struggles openly. "
            "The best sections of the blog come from being honest about what went wrong. "
            "Vulnerability is your superpower."
        ),
        "moderate_purposeful": (
            "VULNERABILITY: Share struggles when there's a clear lesson. "
            "Not gratuitous vulnerability — every failure mentioned leads to an insight the reader can use."
        ),
        "low_solutions_focused": (
            "VULNERABILITY: Focus on solutions and wins. Mention challenges briefly but spend "
            "80% of the writing on what works and how to implement it."
        ),
        "none_authority": (
            "VULNERABILITY: Never share failures publicly. The voice is authoritative and confident. "
            "The reader should feel they're learning from someone who has it figured out."
        ),
    }
    if vu := vulnerabilities.get(dna.get("vulnerability", "")):
        sections.append(vu)

    # EDGE LEVEL
    edges = {
        "clean": (
            "EDGE LEVEL: Clean and professional language throughout. "
            "No edge. No frustration showing. Calm, measured, respectful."
        ),
        "mild_nigerian": (
            "EDGE LEVEL: Mild Nigerian expressions of frustration or emphasis: "
            "'this kind wahala', 'God abeg', 'the audacity'. Human, relatable, but not aggressive."
        ),
        "moderate": (
            "EDGE LEVEL: When something deserves criticism, say it clearly. "
            "'This advice is BS' or 'Stop wasting your money on this.' Direct frustration is authentic."
        ),
        "bold_raw": (
            "EDGE LEVEL: Raw emotional energy comes through in the writing. "
            "When you're frustrated, the reader feels it. When you're excited, it's infectious. "
            "Nothing is sanitised."
        ),
    }
    if ed := edges.get(dna.get("edge", "")):
        sections.append(ed)

    # REFERENCES
    references = {
        "nigerian_business": (
            "REFERENCES: Reference Nigerian business context: Dangote, Flutterwave, Paystack, "
            "Lagos hustle, Nigerian market dynamics, local competitors, Naira economics."
        ),
        "global_tech": (
            "REFERENCES: Reference global business and tech: Apple, Y Combinator, "
            "Silicon Valley strategies, Harvard research, global market trends."
        ),
        "personal_local": (
            "REFERENCES: Reference personal life and local experience: your neighbourhood, "
            "your family, everyday Lagos life, market scenes, traffic, generator wahala."
        ),
        "pop_culture": (
            "REFERENCES: Reference Nigerian pop culture: Nollywood, Afrobeats, viral tweets, "
            "trending memes, cultural moments. The reader should feel like you consume the same internet they do."
        ),
    }
    if re_val := references.get(dna.get("references", "")):
        sections.append(re_val)

    # READER RELATIONSHIP
    roles = {
        "authority": (
            "READER RELATIONSHIP: Write as the expert. "
            "The reader comes to you for definitive answers. Your confidence is your value."
        ),
        "peer": (
            "READER RELATIONSHIP: Write as someone on the same journey. "
            "You're a few steps ahead, sharing what you've learned along the way."
        ),
        "disruptor": (
            "READER RELATIONSHIP: Write as the challenger. "
            "You question the status quo and present your alternative with conviction."
        ),
        "guide": (
            "READER RELATIONSHIP: Write as the patient teacher. You simplify complexity. "
            "The reader should feel clarity after reading, not confusion."
        ),
    }
    if ro := roles.get(dna.get("role", "")):
        sections.append(ro)

    # TEACHING STYLE
    teachings = {
        "narrative_experiential": (
            "TEACHING: Explain through story and experience. Don't describe a concept — "
            "walk the reader through a situation where it played out."
        ),
        "structured_logical": (
            "TEACHING: Break it down step by step. Clear logic, numbered points when needed. "
            "The reader should be able to follow the argument exactly."
        ),
        "analogy_based": (
            "TEACHING: Use analogies the reader already understands. Connect the unfamiliar to the familiar. "
            "Good analogies are worth 500 words of explanation."
        ),
        "myth_busting": (
            "TEACHING: Challenge what the reader currently believes, then show the better way. "
            "Start with the wrong assumption, demolish it, rebuild with the correct one."
        ),
    }
    if te := teachings.get(dna.get("teaching", "")):
        sections.append(te)

    # JARGON
    jargons = {
        "heavy_insider": (
            "JARGON: Use industry terminology freely. "
            "Your readers are insiders. Insider language signals expertise and belonging."
        ),
        "moderate_with_context": (
            "JARGON: Use industry terms but explain them briefly in-line. "
            "Not everyone is an expert yet."
        ),
        "none_accessible": (
            "JARGON: Plain language throughout. "
            "If a 16-year-old can't follow it, simplify. Write for everyone."
        ),
        "deliberate_translate": (
            "JARGON: Use technical terms deliberately, then immediately translate to plain language. "
            "'CAC (the cost to acquire one customer).' Shows expertise without gatekeeping."
        ),
    }
    if ja := jargons.get(dna.get("jargon", "")):
        sections.append(ja)

    # CONFRONTATION / DISAGREEMENT
    confrontations = {
        "diplomatic": (
            "DISAGREEMENT: When you challenge conventional wisdom, present your alternative view "
            "respectfully with evidence. You disagree without attacking."
        ),
        "blunt": (
            "DISAGREEMENT: Say it directly when advice is wrong. 'This is wrong. Here's why.' "
            "No softening. Truth over tact."
        ),
        "narrative": (
            "DISAGREEMENT: Tell the story of what happened when you followed the bad advice. "
            "Let experience make the argument."
        ),
        "avoidant": (
            "DISAGREEMENT: Focus on presenting your perspective. "
            "Don't engage directly with opposing views — let your results speak."
        ),
    }
    if co := confrontations.get(dna.get("confrontation", "")):
        sections.append(co)

    # CLOSING
    closings = {
        "empowering": (
            "CLOSING: End by handing control to the reader. "
            "'The ball is in your court.' Make them feel capable and ready."
        ),
        "prescriptive": (
            "CLOSING: End with a specific, concrete next step. "
            "'Here's what to do in the next 24 hours: [action].' No vague inspiration."
        ),
        "vulnerable_honest": (
            "CLOSING: End honestly. 'I'm still figuring this out too. But so far, this is working.' "
            "The reader leaves feeling like you're on the journey together."
        ),
        "community_shareable": (
            "CLOSING: End with a statement or question that makes the reader want to share. "
            "Something that resonates beyond the individual reader."
        ),
    }
    if cl := closings.get(dna.get("closing", "")):
        sections.append(cl)

    # ASPIRATIONAL INFLUENCE
    if asp := dna.get("aspirational", "").strip():
        sections.append(
            f"ASPIRATIONAL INFLUENCE: Borrow stylistic elements from: {asp}. "
            "Not copying — channelling the energy and approach."
        )

    return "\n\n".join(s for s in sections if s)


class WritingDNAService:
    """Generates and manages Writing DNA voice profiles for blog generation."""

    @staticmethod
    async def analyze_writing_sample(sample_text: str) -> Dict[str, Any]:
        """
        Analyse a pasted writing sample to extract concrete voice patterns.
        These override quiz answers where they conflict — real writing beats stated preference.
        """
        if not sample_text or not sample_text.strip():
            return {}

        prompt = (
            "Analyse this writing sample and extract the writer's patterns. "
            "Return ONLY valid JSON with these exact keys:\n\n"
            '{\n'
            '  "avg_sentence_length": <words per sentence, integer>,\n'
            '  "uses_pidgin": <true/false>,\n'
            '  "pidgin_level": "none|light|moderate|heavy",\n'
            '  "humour_detected": <true/false>,\n'
            '  "humour_type": "none|dry_subtle|natural_witty|bold_playful",\n'
            '  "uses_data": <true/false>,\n'
            '  "vocabulary": "simple|conversational|technical|mixed",\n'
            '  "tone": "serious|warm|playful|bold|sarcastic|motivational",\n'
            '  "notable_patterns": ["list of distinctive writing habits"]\n'
            "}\n\n"
            "Be specific. Extract ACTUAL patterns from the text."
        )

        try:
            response = await _client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": sample_text[:2000]},
                ],
                max_tokens=400,
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            analysis = json.loads(content)
            print(f"📊 Writing sample analysis: tone={analysis.get('tone')}, pidgin={analysis.get('pidgin_level')}")
            return analysis
        except Exception as e:
            print(f"⚠️ Writing sample analysis failed: {e}")
            return {}

    @staticmethod
    async def analyze_aspirational_writers(writers_text: str) -> Dict[str, Any]:
        """
        Look up the writing style of named writers/creators and extract stylistic elements
        the user can channel in their blog posts.
        """
        if not writers_text or not writers_text.strip():
            return {}

        prompt = (
            f"The user admires the writing style of: {writers_text}\n\n"
            "Describe the writing style characteristics of these writers/creators in detail. "
            "Return ONLY valid JSON:\n"
            '{\n'
            '  "writers": ["name1", "name2"],\n'
            '  "shared_traits": ["trait1", "trait2", ...],\n'
            '  "sentence_style": "short/medium/long/varied",\n'
            '  "tone": "description",\n'
            '  "structural_habits": ["habit1", "habit2"],\n'
            '  "style_summary": "1-2 sentence summary the AI can channel"\n'
            "}"
        )

        try:
            response = await _client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            analysis = json.loads(content)
            print(f"✍️  Aspirational writer analysis: {analysis.get('style_summary', '')[:80]}")
            return analysis
        except Exception as e:
            print(f"⚠️ Aspirational writer analysis failed: {e}")
            return {}

    @staticmethod
    def _apply_sample_overrides(dna: Dict[str, str], sample_analysis: Dict[str, Any]) -> Dict[str, str]:
        """Override DNA keys with signals extracted from the actual writing sample."""
        if not sample_analysis:
            return dna

        updated = dna.copy()

        avg_len = sample_analysis.get("avg_sentence_length")
        if avg_len is not None:
            if avg_len < 8:
                updated["pacing"] = "staccato"
                updated["structure"] = "short_punchy_confident"
            elif avg_len > 20:
                updated["pacing"] = "flowing"
                updated["structure"] = "flowing_personal_reflective"

        pidgin = sample_analysis.get("pidgin_level")
        if pidgin and pidgin != "none":
            updated["pidgin"] = pidgin

        if sample_analysis.get("humour_detected") and sample_analysis.get("humour_type"):
            updated["humour"] = sample_analysis["humour_type"]

        if sample_analysis.get("uses_data"):
            updated["structure"] = "data_driven_precise"

        return updated

    @staticmethod
    async def generate_dna(
        answers: Dict[str, str],
        writing_sample: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Full DNA generation pipeline:
        1. Map quiz answers to semantic keys
        2. Analyse writing sample if provided
        3. Look up aspirational writers if Q16 is filled
        4. Override quiz-derived keys with sample analysis
        5. Build the directive DNA prompt string
        """
        dna = _map_answers(answers)

        sample_analysis: Dict[str, Any] = {}
        aspirational_analysis: Dict[str, Any] = {}

        # Run sample analysis and writer lookup concurrently
        import asyncio
        tasks = []
        do_sample = bool(writing_sample and writing_sample.strip())
        do_writers = bool(dna.get("aspirational", "").strip())

        if do_sample:
            tasks.append(WritingDNAService.analyze_writing_sample(writing_sample))
        if do_writers:
            tasks.append(WritingDNAService.analyze_aspirational_writers(dna["aspirational"]))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            idx = 0
            if do_sample:
                sample_analysis = results[idx] if not isinstance(results[idx], Exception) else {}
                idx += 1
            if do_writers:
                aspirational_analysis = results[idx] if not isinstance(results[idx], Exception) else {}

        # Apply sample overrides
        if sample_analysis:
            dna = WritingDNAService._apply_sample_overrides(dna, sample_analysis)

        # Enrich aspirational key with the style summary if available
        if aspirational_analysis.get("style_summary"):
            dna["aspirational"] = (
                f"{dna.get('aspirational', '')} — {aspirational_analysis['style_summary']}"
            ).strip(" —")

        prompt = _build_dna_prompt(dna)

        return {
            "dna_keys": dna,
            "sample_analysis": sample_analysis,
            "aspirational_analysis": aspirational_analysis,
            "writing_dna_prompt": prompt,
        }

    @staticmethod
    async def save(
        user_id: str,
        quiz_answers: Dict[str, str],
        writing_sample: Optional[str],
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """Generate and persist Writing DNA for a user."""
        result = await WritingDNAService.generate_dna(quiz_answers, writing_sample)

        now = datetime.utcnow()
        doc = {
            "user_id": user_id,
            "quiz_answers": quiz_answers,
            "sample_text": writing_sample or "",
            "sample_analysis": result["sample_analysis"],
            "aspirational_writers": [
                w.strip()
                for w in quiz_answers.get("q16", "").split(",")
                if w.strip()
            ],
            "aspirational_analysis": result["aspirational_analysis"],
            "dna_keys": result["dna_keys"],
            "writing_dna_prompt": result["writing_dna_prompt"],
            "updated_at": now,
        }

        existing = await db[COLLECTION].find_one({"user_id": user_id})
        if existing:
            await db[COLLECTION].update_one({"user_id": user_id}, {"$set": doc})
        else:
            doc["created_at"] = now
            await db[COLLECTION].insert_one(doc)

        saved = await db[COLLECTION].find_one({"user_id": user_id})
        if saved:
            saved.pop("_id", None)

        # Update brand profile with writing_dna_id reference (spec §7)
        try:
            dna_raw = await db[COLLECTION].find_one({"user_id": user_id}, {"_id": 1})
            if dna_raw:
                await db["brand_profiles"].update_one(
                    {"user_id": user_id},
                    {"$set": {"writing_dna_id": str(dna_raw["_id"]), "updated_at": now}},
                )
        except Exception as e:
            print(f"⚠️ Could not update brand profile writing_dna_id: {e}")

        print(f"✅ Writing DNA saved for user={user_id}")
        return UriResponse.create_response("writing_dna", saved)

    @staticmethod
    async def get(user_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        doc = await db[COLLECTION].find_one({"user_id": user_id})
        if not doc:
            return UriResponse.get_single_data_response("writing_dna", None)
        doc.pop("_id", None)
        return UriResponse.get_single_data_response("writing_dna", doc)

    @staticmethod
    async def get_prompt(user_id: str, db: AsyncIOMotorDatabase) -> Optional[str]:
        """Return just the DNA prompt string, or None if not set up."""
        doc = await db[COLLECTION].find_one({"user_id": user_id}, {"writing_dna_prompt": 1})
        if not doc:
            return None
        return doc.get("writing_dna_prompt") or None

    @staticmethod
    async def learn_from_edits(
        user_id: str,
        original_content: str,
        edited_content: str,
        db: AsyncIOMotorDatabase,
    ) -> None:
        """
        Compare the AI-generated blog with the user's edited version.
        Extract what changed and append learned adjustments to the DNA prompt.
        Runs as a background task — errors are logged but not raised.
        """
        if not original_content or not edited_content:
            return
        if original_content.strip() == edited_content.strip():
            return

        system_prompt = (
            "Compare the original AI-generated blog with the user's edited version. "
            "Identify patterns in what the user changed. "
            "Return ONLY valid JSON:\n"
            '{\n'
            '  "changes": [\n'
            '    {"type": "tone|structure|vocabulary|pidgin|humour|other", "pattern": "description"}\n'
            '  ],\n'
            '  "voice_adjustments": ["specific 1-sentence adjustment to make to the Writing DNA"]\n'
            "}"
        )

        try:
            response = await _client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"ORIGINAL:\n{original_content[:3000]}"
                            f"\n\nEDITED BY USER:\n{edited_content[:3000]}"
                        ),
                    },
                ],
                max_tokens=500,
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            result = json.loads(content)
            adjustments: List[str] = result.get("voice_adjustments", [])

            if adjustments:
                learned_block = "\n\nLEARNED FROM YOUR EDITS:\n" + "\n".join(
                    f"- {adj}" for adj in adjustments[:5]
                )
                await db[COLLECTION].update_one(
                    {"user_id": user_id},
                    {"$set": {"updated_at": datetime.utcnow()},
                     "$push": {"learning_history": {
                         "adjustments": adjustments,
                         "learned_at": datetime.utcnow(),
                     }}},
                )
                # Append adjustments to the DNA prompt text
                await db[COLLECTION].update_one(
                    {"user_id": user_id},
                    {"$set": {"updated_at": datetime.utcnow()}},
                )
                doc = await db[COLLECTION].find_one({"user_id": user_id})
                if doc:
                    existing_prompt = doc.get("writing_dna_prompt", "")
                    updated_prompt = existing_prompt + learned_block
                    await db[COLLECTION].update_one(
                        {"user_id": user_id},
                        {"$set": {"writing_dna_prompt": updated_prompt}},
                    )
                    print(f"🧠 Writing DNA updated with {len(adjustments)} learned adjustment(s) for user={user_id}")

        except Exception as e:
            print(f"⚠️ learn_from_edits failed for user={user_id}: {e}")

    @staticmethod
    async def accumulate_published_sample(
        user_id: str,
        published_content: str,
        db: AsyncIOMotorDatabase,
    ) -> None:
        """
        Spec §8.3 — Sample Accumulation.
        Every published blog (after user edits) becomes a writing sample.
        Analyses the final published content and appends concrete voice patterns
        to the DNA prompt so future generations improve over time.
        """
        if not published_content or not published_content.strip():
            return

        try:
            analysis = await WritingDNAService.analyze_writing_sample(published_content[:2000])
            if not analysis:
                return

            insights: List[str] = []
            if analysis.get("tone"):
                insights.append(f"your natural tone in published work is {analysis['tone']}")
            avg = analysis.get("avg_sentence_length")
            if avg:
                insights.append(f"your published sentences average {avg} words — keep matching this")
            for pattern in (analysis.get("notable_patterns") or [])[:2]:
                insights.append(pattern)

            if insights:
                sample_block = "\n\nLEARNED FROM PUBLISHED POST:\n" + "\n".join(
                    f"- {ins}" for ins in insights[:4]
                )
                doc = await db[COLLECTION].find_one({"user_id": user_id})
                if doc:
                    updated_prompt = doc.get("writing_dna_prompt", "") + sample_block
                    await db[COLLECTION].update_one(
                        {"user_id": user_id},
                        {
                            "$set": {"writing_dna_prompt": updated_prompt, "updated_at": datetime.utcnow()},
                            "$push": {
                                "published_samples": {
                                    "insights": insights,
                                    "published_at": datetime.utcnow(),
                                }
                            },
                        },
                    )
                    print(f"📚 DNA enriched from published post for user={user_id} ({len(insights)} insights)")

        except Exception as e:
            print(f"⚠️ accumulate_published_sample failed for user={user_id}: {e}")
