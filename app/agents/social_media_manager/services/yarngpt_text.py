"""
Turn written text into plain spoken words before it is sent to a text-to-speech engine.

"₦31,000" must be said as "31000 naira", "50%" as "50 percent", "&" as "and", and "AI" as the
letters A I; left alone, an engine may skip the symbol, read the comma as a break, or pronounce
"AI" as a word. Pure functions only, no network, so it is cheap to test.
"""
import re

_CURRENCY = {"₦": "naira", "$": "dollars", "£": "pounds", "€": "euros"}

# Letters people write in capitals that must be spelled out, not read as a word.
_SPELLED = {"AI": "A I", "TV": "T V", "SMS": "S M S", "FAQ": "F A Q", "CEO": "C E O", "ID": "I D"}

_ABBREVIATIONS = [
    (re.compile(r"\bMr\.", re.I), "mister"),
    (re.compile(r"\bMrs\.", re.I), "missus"),
    (re.compile(r"\bDr\.", re.I), "doctor"),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
]


def normalize_for_speech(text: str, strip_punctuation: bool = False) -> str:
    """`strip_punctuation` also drops apostrophes and turns dashes/slashes into spaces. Leave it
    off for an engine that reads punctuation for rhythm; turn it on for one (like YarnGPT's open
    model) that deletes every non-letter, where "don't" would otherwise become "dont" by accident."""
    text = (text or "").strip()
    if not text:
        return ""

    # Currency written before a number: "₦31,000" -> "31,000 naira" (the order people say it in).
    text = re.sub(r"([₦$£€])\s?(\d[\d,]*(?:\.\d+)?)",
                  lambda m: f"{m.group(2)} {_CURRENCY[m.group(1)]}", text)
    text = re.sub(r"\bN(\d[\d,]*(?:\.\d+)?)\b", lambda m: f"{m.group(1)} naira", text)

    # "31,000" read as "31" and "000" would be a different amount entirely.
    text = re.sub(r"\d[\d,]*(?:\.\d+)?", lambda m: m.group(0).replace(",", ""), text)
    text = re.sub(r"(\d)\s?%", r"\1 percent", text)
    text = text.replace("&", " and ").replace("+", " plus ").replace("@", " at ")

    for pattern, spoken in _ABBREVIATIONS:
        text = pattern.sub(spoken, text)
    for caps, spoken in _SPELLED.items():
        text = re.sub(rf"\b{caps}\b", spoken, text)

    if strip_punctuation:
        text = text.replace("'", "").replace("’", "")
        text = re.sub(r"[-–—/]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
