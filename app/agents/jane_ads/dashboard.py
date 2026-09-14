"""
Jane + Ads — the Home surface (DASH-PRD-01 §4).

A dashboard for someone who does not want a dashboard. The governing rule from the
PRD (§0) is that every component answers a question the client actually wonders,
and anything that exists because an API returns it is cut. So this module builds
exactly four answers and nothing else:

  1. Did anyone message me?      → since_you_last_looked
  2. Is my campaign working?     → live campaigns strip
  3. How much money is left?     → money line
  4. What should I do now?       → suggestions, maximum three

Two rules are enforced here rather than left to the frontend, because both are the
kind that quietly stop being true otherwise:

**Conversations are suppressed, never zeroed.** Meta reports a conversation count
only for native Click-to-WhatsApp ads, so a wa.me fallback campaign reads 0 for its
whole life — which a client reads as "my ad did nothing" when it actually means "we
cannot see this". measurability.py owns that distinction; nothing here prints a
count it did not clear.

**At most three suggestions, and zero where nothing qualifies** (§4.3). A padded
suggestion block is less trustworthy than an empty one, so there is no filler: if
nothing is genuinely worth saying, the list comes back empty and the UI shows
nothing.

Deliberately NOT here: anything needing per-person message threads (§4.1's "still
waiting for a reply", the whole Messages surface). Meta gives aggregate counts only —
adapters/meta.py's poll_conversations says so explicitly — so the waiting count has
no data source until the WhatsApp Cloud API work lands. Showing it as 0 would be the
same lie this module exists to avoid.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import constants as C
from . import measurability as M

LAST_SEEN_COLLECTION = "jane_ads_dashboard_last_seen"
MAX_SUGGESTIONS = 3          # §4.3, a hard cap — more becomes a to-do list nobody clears

# Below this the wallet can't fund even a minimum campaign, so the money line turns
# amber (§4.4 "turns amber below one campaign's worth").
LOW_WALLET_NGN = C.MIN_TOPUP_NGN


def _as_aware_utc(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value if isinstance(value, datetime) else None


async def read_last_seen(db, brand_id: str) -> Optional[datetime]:
    """When this brand last opened Home. None on a first visit, which callers must
    render as "no delta yet" rather than as a zero (§12: never show a zero)."""
    if db is None or not brand_id:
        return None
    doc = await db[LAST_SEEN_COLLECTION].find_one({"brand_id": brand_id}, {"_id": 0, "seen_at": 1})
    return _as_aware_utc((doc or {}).get("seen_at"))


async def mark_seen(db, brand_id: str, when: Optional[datetime] = None) -> None:
    """Record this visit, so the NEXT one can show a delta.

    Written after the response is built, never before — otherwise opening Home would
    clear the very delta the client came to read.
    """
    if db is None or not brand_id:
        return
    await db[LAST_SEEN_COLLECTION].update_one(
        {"brand_id": brand_id},
        {"$set": {"brand_id": brand_id, "seen_at": when or datetime.now(timezone.utc)}},
        upsert=True,
    )


def since_you_last_looked(campaign_rows: list[dict], last_seen: Optional[datetime]) -> dict:
    """§4.1 — what changed since the client last looked.

    Absolute totals mean nothing to someone who checks weekly ("147 conversations"
    tells them nothing); a delta is immediately interpretable. Campaigns that cannot
    report conversations are excluded from the count AND surfaced separately, so the
    headline is never quietly depressed by ads we simply can't see.
    """
    people_messaged = 0
    counted, uncountable = 0, 0
    for row in campaign_rows:
        metrics = row.get("metrics") or {}
        if row.get("conversations_reportable"):
            counted += 1
            people_messaged += int(metrics.get("conversations") or 0)
        elif (row.get("destination_type") or "") == "whatsapp":
            uncountable += 1
    return {
        # None (not 0) on a first visit: there is no "since" to speak of yet.
        "since": last_seen.isoformat() if last_seen else None,
        "people_messaged": people_messaged if counted else None,
        "countable_campaigns": counted,
        # How many WhatsApp campaigns are running that we genuinely cannot measure —
        # the honest footnote under the headline, rather than silently under-reporting.
        "uncountable_campaigns": uncountable,
    }


def live_campaign_strip(campaign_rows: list[dict]) -> list[dict]:
    """§4.2 — what is running right now, and what it has cost so far.

    Money leaving the wallet is what produces anxiety, so spend is the prominent
    figure, expressed against the budget the CLIENT stated (never the ad spend that
    reached the platform — see constants.stated_budget_from_record).
    """
    live = []
    for row in campaign_rows:
        if (row.get("status") or "").lower() not in ("active", "in review", "pending_review"):
            continue
        metrics = row.get("metrics") or {}
        live.append({
            "campaign_id": row.get("campaign_id"),
            "name": row.get("name") or "Campaign",
            "status": row.get("status"),
            "budget_ngn": row.get("budget_ngn"),
            "spent_ngn": metrics.get("spend_ngn"),
            "ends_at": metrics.get("ends_at"),
            # None where unmeasurable — the UI must omit the line, not print a zero.
            "people_messaged": metrics.get("conversations"),
            "conversations_reportable": bool(row.get("conversations_reportable")),
            "image_url": row.get("image_url") or "",
        })
    return live


def money_line(balance_ngn: float, credits: Optional[int]) -> dict:
    """§4.4 — answers "how much have I got left" in eight characters."""
    return {
        "wallet_ngn": round(float(balance_ngn or 0), 2),
        "credits": credits,
        # Amber below one campaign's worth, so the client learns it before planning
        # rather than at launch, which is the worst possible moment to find out.
        "low": float(balance_ngn or 0) < LOW_WALLET_NGN,
        "min_topup_ngn": C.MIN_TOPUP_NGN,
    }


def build_suggestions(campaign_rows: list[dict], money: dict) -> list[dict]:
    """§4.3 — what to DO, at most three, ordered by how much it costs to ignore.

    The PRD calls this the most important component on the dashboard: our user does
    not want to interpret data, they want to know what to do next. Every entry is one
    tap from acting.

    Ordering is by consequence, not by recency: money already spent on an ad that
    cannot be measured, or a wallet that will block the next launch, outranks
    anything informational. Where nothing qualifies this returns EMPTY — §4.3 is
    explicit that an empty block is more trustworthy than a padded one.
    """
    suggestions: list[dict] = []

    # A running WhatsApp campaign whose messages we cannot count. Highest value
    # because it is live, costing money, and fixable in a few minutes — and because
    # the client would otherwise read its zero as failure.
    for row in campaign_rows:
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
        if (row.get("status") or "").lower() != "active":
            continue
        if row.get("conversations_reportable"):
            continue
        if (row.get("destination_type") or "") != "whatsapp":
            continue
        # ONLY a campaign we know is a wa.me fallback. A campaign that merely predates
        # the measurability stamp is UNKNOWN — telling its owner to go link a number
        # that may already be linked (live case: a native campaign launched hours
        # before the stamp existed) is advice we cannot stand behind, and it sends
        # them to fix something that isn't broken.
        if row.get("conversations_state") != M.UNMEASURABLE:
            continue
        suggestions.append({
            "kind": "link_whatsapp_number",
            "text": f"We can't count messages from “{row.get('name') or 'your campaign'}”",
            "detail": M.unreportable_reason(row),
            "action_label": "Fix this",
            "action": "connections",
            "campaign_id": row.get("campaign_id"),
        })
        break   # one per dashboard, not one per campaign — the cap is load-bearing

    # Spending with nothing to show for it. Only ever raised where the count is real:
    # an unmeasurable campaign has no evidence either way and must not be called bad.
    for row in campaign_rows:
        if len(suggestions) >= MAX_SUGGESTIONS:
            break
        metrics = row.get("metrics") or {}
        if (row.get("status") or "").lower() != "active":
            continue
        if not row.get("conversations_reportable"):
            continue
        if float(metrics.get("spend_ngn") or 0) <= 0:
            continue
        if int(metrics.get("conversations") or 0) > 0:
            continue
        suggestions.append({
            "kind": "campaign_quiet",
            "text": f"“{row.get('name') or 'Your campaign'}” is spending but nobody has messaged yet",
            "detail": "Ask Jane to look at it — the audience or the offer may need a change.",
            "action_label": "Ask Jane",
            "action": "ask_jane",
            "campaign_id": row.get("campaign_id"),
        })
        break

    # Wallet too low to launch anything. Last because it blocks the NEXT campaign
    # rather than damaging a running one.
    if len(suggestions) < MAX_SUGGESTIONS and money.get("low"):
        suggestions.append({
            "kind": "wallet_low",
            "text": "Your wallet is too low to start a new campaign",
            "detail": f"Top up at least ₦{C.MIN_TOPUP_NGN:,.0f} to launch again.",
            "action_label": "Top up",
            "action": "wallet",
            "campaign_id": "",
        })

    return suggestions[:MAX_SUGGESTIONS]
