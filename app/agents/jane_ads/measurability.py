"""
Jane + Ads — what a campaign's numbers are actually allowed to claim.

DASH-PRD-01 §9 makes "X people messaged you" the headline metric on every surface,
and §15 requires every number to be stateable in a sentence a market vendor would
use. Both break on the same fact: Meta fires
onsite_conversion.messaging_conversation_started ONLY for native Click-to-WhatsApp
ads. A wa.me link ad — the fallback taken whenever the brand's number isn't linked
to their Page — reports 0 conversations for its entire life.

That zero is not "nobody messaged you". It is "we cannot see who messaged you", and
those are opposite messages to a client deciding whether their ad worked. Live count
at the time of writing: of 21 campaigns on the real ad account, exactly 1 was native.
A dashboard that printed the raw metric would have told 20 clients their ads produced
nothing.

So conversations are reported only when the campaign could genuinely produce them,
and suppressed — not zeroed — otherwise. This mirrors the outcome coverage floor in
DASH-PRD-01 §6.2: below the floor, show what is known and omit the claim rather than
extrapolating.

`conversations_measurable` is stamped at launch (adapters/meta.py) because that is
the only moment the native-vs-fallback decision is known without re-reading the ad
set from Meta. Records predating the stamp are UNKNOWN, not False: most were built
before native was attempted at all, but some were native, and asserting either way
about a real client's campaign on a guess is exactly the failure this module exists
to prevent.
"""
from __future__ import annotations

from typing import Optional

# What a campaign is allowed to say about conversations.
MEASURABLE = "measurable"       # native Click-to-WhatsApp — the count is real
UNMEASURABLE = "unmeasurable"   # wa.me link ad — Meta will never report one
UNKNOWN = "unknown"             # predates the stamp, or not a WhatsApp campaign


def conversation_state(record: dict) -> str:
    """Whether this campaign's conversation count means anything.

    Reads the flag stamped at launch. Absent means the campaign predates it, which
    is reported as UNKNOWN so callers suppress the claim instead of inventing one.
    """
    flag = record.get("conversations_measurable")
    if flag is True:
        return MEASURABLE
    if flag is False:
        return UNMEASURABLE
    return UNKNOWN


def conversations_reportable(record: dict) -> bool:
    """Whether "X people messaged you" may be shown for this campaign at all."""
    return conversation_state(record) == MEASURABLE


def conversation_count(record: dict, raw_count: Optional[int]) -> Optional[int]:
    """The conversation count to display, or None where it must be suppressed.

    None is deliberately not 0: the caller must render "we can't count messages on
    this one", never "0 people messaged you".
    """
    if not conversations_reportable(record):
        return None
    return int(raw_count or 0)


def unreportable_reason(record: dict) -> str:
    """Plain-language explanation for a suppressed count, in the client's own terms —
    no "native", no "CTWA", no metric names (DASH-PRD-01 §9's translation rule)."""
    state = conversation_state(record)
    if state == UNMEASURABLE:
        return (
            "This ad sends people to WhatsApp through a link, so we can't count the "
            "messages it brought in. Link your WhatsApp number to your Facebook Page "
            "and future ads will count them."
        )
    if state == UNKNOWN:
        return "We can't count messages on this campaign — it ran before we could track them."
    return ""
