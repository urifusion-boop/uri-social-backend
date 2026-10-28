"""
Text cleanup for YarnGPT.

YarnGPT's own tokenizer lower-cases the text and deletes everything that is not a letter or
a space, after turning digits into words. So "₦31,000" would be spoken as "thirty one
thousand" with no mention of naira, "&" and "%" vanish, and "don't" becomes "dont". This turns
what a customer or Jane writes into plain spoken words first, so the model hears what was meant.

Pure functions only, no model or network, so it is cheap to test.
"""
import re

_CURRENCY = {"₦": "naira", "N": "naira", "$": "dollars", "£": "pounds", "€": "euros"}

# Letters people write in capitals that must be spelled out, not read as a word.
_SPELLED = {"AI": "A I", "TV": "T V", "SMS": "S M S", "FAQ": "F A Q", "CEO": "C E O", "ID": "I D"}

_ABBREVIATIONS = [
    (re.compile(r"\bMr\.", re.I), "mister"),
    (re.compile(r"\bMrs\.", re.I), "missus"),
    (re.compile(r"\bDr\.", re.I), "doctor"),
    (re.compile(r"\bvs\.?\b", re.I), "versus"),
]


def _number_words(match: "re.Match") -> str:
    """'31,000' -> '31000' so the later digit-to-words step reads one number, not two."""
    return match.group(0).replace(",", "")


def normalize_for_speech(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    # Currency written before a number: "₦31,000" -> "31,000 naira" (order matters in speech).
    text = re.sub(r"[₦$£€]\s?(\d[\d,]*(?:\.\d+)?)",
                  lambda m: f"{m.group(1)} {_CURRENCY[text[m.start()]]}", text)
    text = re.sub(r"\bN(\d[\d,]*(?:\.\d+)?)\b", lambda m: f"{m.group(1)} naira", text)

    text = re.sub(r"\d[\d,]*(?:\.\d+)?", _number_words, text)
    text = re.sub(r"(\d)\s?%", r"\1 percent", text)
    text = text.replace("&", " and ").replace("+", " plus ").replace("@", " at ")

    for pattern, spoken in _ABBREVIATIONS:
        text = pattern.sub(spoken, text)
    for caps, spoken in _SPELLED.items():
        text = re.sub(rf"\b{caps}\b", spoken, text)

    # Apostrophes would be deleted letter-wise ("don't" -> "dont"); drop them deliberately.
    text = text.replace("'", "").replace("’", "")
    # Dashes and slashes separate words; the tokenizer would otherwise glue or delete them.
    text = re.sub(r"[-–—/]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
