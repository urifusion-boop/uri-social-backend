"""
30-day Content Calendar follows the Brand Playbook's hard rules: the plan is
generated with the guardrails, content pillars and the brand's key dates, and
every banned word (guardrails, voice profile, words to avoid) is checked in
all its common forms.
"""
from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc
from app.agents.social_media_manager.services import brand_brief as bb

BRAND = {
    "brand_name": "Glow Hair",
    "guardrails": {"avoid_topics": "politics", "banned_words": "cheap, streamline",
                   "compliance_notes": "No medical claims", "emoji_usage": "None", "max_hashtags": "5"},
    "voice_profile": {"banned_words": ["synergy"]},
    "content_pillars": ["Hair care tips", "Styling", "Behind the scenes"],
    "key_dates": "Store anniversary: 12 March",
    "languages": ["English", "Pidgin"],
}


def test_banned_words_come_from_every_list_without_duplicates():
    words = bb.banned_words({**BRAND, "words_to_avoid": ["Cheap", "guarantee"]})
    assert words == ["synergy", "Cheap", "guarantee", "streamline"]


def test_banned_word_forms_are_caught_but_not_inside_other_words():
    assert bb.find_banned("We are streamlining everything", ["streamline"]) == ["streamline"]
    assert bb.find_banned("Book your slot", ["ok"]) == []
    assert bb.find_banned("Cheaper wigs", ["cheap"]) == ["cheap"]


def test_hard_rules_from_prod_guardrails():
    rules = bb.hard_rules(BRAND)
    text = " ".join(rules)
    assert "NEVER mention or reference: politics." in rules
    assert "synergy, cheap, streamline" in text and "every form of each word" in text
    assert "Compliance requirement: No medical claims." in rules
    assert "Use NO emojis under any circumstances." in rules
    assert "English stays the main language" in text               # Pidgin never takes over
    assert "Use a maximum of 5 hashtags." in bb.hard_rules(BRAND, captions=True)


def test_empty_playbook_gives_no_rules():
    assert bb.hard_rules({}) == [] and bb.banned_words(None) == []


def test_plan_prompt_carries_rules_pillars_and_key_dates():
    block = svc._brand_rules_block(BRAND)
    assert "HARD RULES" in block and "NEVER mention or reference: politics." in block
    assert "CONTENT PILLARS: Hair care tips, Styling, Behind the scenes" in block
    assert "IMPORTANT DATES for this brand: Store anniversary: 12 March" in block
    # The generic-AI-word ban always applies, even with no brand-specific
    # rules at all — only the brand-conditional sections (hard rules,
    # pillars, key dates, formats) are empty for an empty brand dict.
    empty_block = svc._brand_rules_block({})
    assert "NEVER use these generic AI words" in empty_block
    assert "HARD RULES" not in empty_block
    assert "CONTENT PILLARS" not in empty_block


def test_calendar_checks_guardrail_banned_words_in_every_form():
    words = svc._all_banned_words(BRAND)
    assert set(words) == {"synergy", "cheap", "streamline"}
    idea = {"title": "Streamlining your morning routine", "hook": "Cheaper than you think", "caption": "x"}
    assert set(svc._find_banned_words(idea, words)) == {"streamline", "cheap"}
    assert svc._find_banned_words({"title": "Book your appointment"}, ["ok"]) == []
