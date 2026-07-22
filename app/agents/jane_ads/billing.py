"""
Jane + Ads — conversation-charge reconciliation (the "customers pay us" loop).

Meta bills URI's own ad account (a real card) as an ACTIVE campaign delivers. This
job recoups that spend from the customer's prepaid wallet, one delivered WhatsApp
conversation at a time, at the wallet's existing dynamic price
(MAX(₦400, trailing platform cost × 1.5) — see wallet.py). It runs periodically off
the notification scheduler.

Why this shape:
  - Per-conversation, not per-Naira: reuses the prepaid pricing already built into
    wallet.charge_conversation / price_conversation, rather than inventing a second
    billing model. `actual_platform_cost_ngn` (Meta's real per-conversation cost) is
    recorded on each charge so the trailing-cost pricing stays self-consistent.
  - Its OWN high-water mark (`conversations_billed`) advances ONLY by conversations
    actually charged. If a wallet runs dry mid-sweep, the unbilled remainder is NOT
    skipped — it stays unbilled and is retried next sweep once the wallet is topped
    up. No silently dropped charges.
  - Spend safety: the moment a charge can't be covered, the campaign is PAUSED on
    Meta (cascade) so URI stops fronting money it can't recoup, and the owner is told
    to top up. Reactivation stays manual — this loop never turns a campaign back on.
"""
from __future__ import annotations

from datetime import datetime, timezone

COLLECTION = "jane_ads_meta_campaigns"


async def _notify(notification_service, *, user_id: str, subject: str, message: str, campaign_id: str) -> None:
    # In-app notification only (what GET /notifications reads) — no email/WhatsApp
    # dispatched here, same as the monitoring sweep.
    await notification_service._log_notification(
        user_id=user_id,
        notification_type="campaign_update",
        channel="email",
        subject=subject,
        status="sent",
        metadata={"campaign_id": campaign_id, "message": message},
    )


async def reconcile_conversation_charges(db) -> dict:
    """One pass over every launched campaign: charge the owner's wallet for each
    newly-delivered conversation, and pause any campaign whose wallet can no longer
    cover the next charge. Best-effort per campaign — one failure never blocks the
    rest. Returns a small summary for logging."""
    from app.core.config import settings
    from app.services.NotificationService import notification_service

    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .store import MongoWalletStore
    from .wallet import InsufficientFundsError, WalletService

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        return {"checked": 0, "charged": 0, "paused": 0}

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    wallet = WalletService(MongoWalletStore(db))
    records = await db[COLLECTION].find({}, {"_id": 0}).to_list(length=500)

    checked = charged_total = paused_total = 0
    for r in records:
        campaign_id = r.get("campaign_id")
        business_id = r.get("business_id")
        user_id = r.get("user_id")
        # A real, owned campaign only — anonymous one-shot ids have no funded wallet
        # and no user to notify, so there's nothing to reconcile.
        if not campaign_id or not business_id or not user_id:
            continue

        try:
            summary = await adapter.fetch_campaign_summary(campaign_id)
        except Exception as e:
            print(f"[billing] summary failed for {campaign_id}: {e}", flush=True)
            continue
        checked += 1

        if summary["delivery"] == "Deleted":
            # Matches GET /meta/campaigns / monitoring: drop the ghost record.
            await db[COLLECTION].delete_one({"campaign_id": campaign_id})
            continue

        billed = int(r.get("conversations_billed", 0))
        total = int(summary.get("conversations", 0))
        unbilled = total - billed
        if unbilled <= 0:
            continue

        # Meta's real per-conversation cost — recorded on each charge so the wallet's
        # trailing-cost pricing reflects reality. Fall back to spend/total if the
        # cost_per_action field wasn't populated yet.
        per_conv_cost = summary.get("cost_per_conversation_ngn")
        if not per_conv_cost and total:
            per_conv_cost = summary.get("spend_ngn", 0.0) / total

        charged_now = 0
        ran_dry = False
        for _ in range(unbilled):
            try:
                await wallet.charge_conversation(
                    business_id, campaign_id=campaign_id, ad_id=r.get("ad_id", ""),
                    actual_platform_cost_ngn=per_conv_cost,
                )
                charged_now += 1
            except InsufficientFundsError:
                ran_dry = True
                break

        # Advance the water mark ONLY by what was actually charged, so the unbilled
        # remainder is retried next sweep (after a top-up) instead of being lost.
        if charged_now:
            await db[COLLECTION].update_one(
                {"campaign_id": campaign_id},
                {"$set": {"conversations_billed": billed + charged_now,
                          "last_billed_at": datetime.now(timezone.utc)}},
            )
            charged_total += charged_now

        if not ran_dry:
            continue

        # Wallet can't cover the next conversation. Stop fronting money we can't
        # recoup: pause on Meta (cascade), flag it, and tell the owner — but only
        # once, and only actually pause if it's still delivering.
        if summary["delivery"] == "Active":
            try:
                await adapter.set_delivery(campaign_id, active=False)
                paused_total += 1
            except MetaAPIError as e:
                print(f"[billing] pause failed for {campaign_id}: {e}", flush=True)

        if not r.get("paused_for_funds"):
            name = r.get("display_name") or "Your campaign"
            await _notify(
                notification_service, user_id=user_id,
                subject=f"{name} is paused — top up to keep it running",
                message=(
                    f"{name} used up your ad wallet, so I paused it to avoid overspending. "
                    "Top up your wallet and reactivate it whenever you're ready to keep going."
                ),
                campaign_id=campaign_id,
            )
            await db[COLLECTION].update_one(
                {"campaign_id": campaign_id},
                {"$set": {"paused_for_funds": True}},
            )

    return {"checked": checked, "charged": charged_total, "paused": paused_total}
