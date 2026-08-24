"""
Jane + Ads — ad-spend reconciliation (the "customers pay us" loop).

Meta and TikTok both bill URI's own ad/advertiser account (a real card) as an
ACTIVE campaign delivers. This job recoups that spend from the customer's prepaid
wallet at `spend × AD_SPEND_MARKUP`, and pauses any campaign whose wallet can no
longer cover it. Runs hourly off the notification scheduler. TikTok's sweep is a
no-op until TIKTOK_ADS_ADVERTISER_ID/TIKTOK_ADS_ACCESS_TOKEN are configured (see
_sweep_platform, shared by both platforms).

Why spend-based (not per-conversation):
  - No basis risk. URI is a reseller fronting a real card — billing on ACTUAL spend
    guarantees it's always made whole plus margin, no matter how a campaign performs.
    Per-conversation billing loses money on any campaign that gets impressions but
    few conversations.
  - Exact, reconcilable tracking. Each platform's `spend` is one monotonic number
    per campaign; the sum across campaigns ties out to the ad account's
    `amount_spent` and the card statement. Conversations are discrete, delayed, and
    re-attributable.
  - Matches the customer's mental model: they funded a budget; it runs until that
    budget is used. Conversations are the reported RESULT, not the billing meter.

Design guarantees:
  - Its OWN high-water mark, `spend_billed_ngn`, advances ONLY by the platform
    spend actually recouped. A wallet that runs dry mid-sweep takes a PARTIAL slice
    (min(owed, balance)); the uncovered remainder stays billable and is charged next
    sweep after a top-up. No silently dropped spend.
  - The instant the wallet can't cover the full slice, the campaign is PAUSED on
    the platform (cascade) so URI stops fronting money it can't recoup, and the
    owner is told once to top up. Reactivation stays manual — this loop never turns
    a campaign back on.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import constants as C

COLLECTION = "jane_ads_meta_campaigns"
TIKTOK_COLLECTION = "jane_ads_tiktok_campaigns"


async def _notify(notification_service, *, user_id: str, subject: str, message: str, campaign_id: str) -> None:
    # In-app notification only (what GET /notifications reads); no email/WhatsApp here.
    await notification_service._log_notification(
        user_id=user_id,
        notification_type="campaign_update",
        channel="email",
        subject=subject,
        status="sent",
        metadata={"campaign_id": campaign_id, "message": message},
    )


async def reconcile_ad_spend_charges(db) -> dict:
    """One pass over every launched campaign, Meta and TikTok both: debit the
    owner's wallet for the new platform spend since last sweep (× markup), and
    pause any campaign whose wallet can no longer cover it. Best-effort per
    campaign — one failure never blocks the rest. Returns a small combined summary
    for logging (amounts in Naira)."""
    from app.core.config import settings
    from app.services.NotificationService import notification_service

    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .adapters.tiktok import TikTokAdsAdapter, TikTokAdsAPIError
    from .store import MongoWalletStore
    from .wallet import WalletService

    wallet = WalletService(MongoWalletStore(db))
    result = {"checked": 0, "charged_ngn": 0.0, "paused": 0}

    if settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN:
        meta_adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
        meta_result = await _sweep_platform(
            db, wallet, notification_service, adapter=meta_adapter,
            collection=COLLECTION, api_error_cls=MetaAPIError,
        )
        for k in result:
            result[k] += meta_result[k]

    if settings.TIKTOK_ADS_ADVERTISER_ID and settings.TIKTOK_ADS_ACCESS_TOKEN:
        tiktok_adapter = TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID, access_token=settings.TIKTOK_ADS_ACCESS_TOKEN)
        tiktok_result = await _sweep_platform(
            db, wallet, notification_service, adapter=tiktok_adapter,
            collection=TIKTOK_COLLECTION, api_error_cls=TikTokAdsAPIError,
        )
        for k in result:
            result[k] += tiktok_result[k]

    return result


async def _sweep_platform(db, wallet, notification_service, *, adapter, collection: str, api_error_cls) -> dict:
    """One platform's sweep — the exact loop reconcile_ad_spend_charges always ran,
    now parameterized by adapter/collection/error-class so Meta and TikTok share it
    instead of duplicating the whole thing. `adapter` must expose
    fetch_campaign_summary/set_delivery (not part of the AdPlatformAdapter ABC, a
    convention both adapters follow — see their own docstrings)."""
    from .wallet import InsufficientFundsError

    markup = C.AD_SPEND_MARKUP
    records = await db[collection].find({}, {"_id": 0}).to_list(length=500)

    checked = paused_total = 0
    charged_total = 0.0
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
            await db[collection].delete_one({"campaign_id": campaign_id})
            continue

        platform_spend = float(summary.get("spend_ngn", 0.0))
        prior = r.get("spend_billed_ngn", 0.0)   # raw stored mark — used as the claim guard
        billed = float(prior or 0.0)
        new_spend = round(platform_spend - billed, 2)
        if new_spend <= 0:
            continue

        owed = round(new_spend * markup, 2)                 # what the customer owes us
        balance = await wallet.get_balance(business_id)
        charge = round(min(owed, balance), 2)               # what leaves the wallet — never exceeds it
        ran_dry = charge < owed

        if charge > 0:
            # Platform spend this charge recoups (charge ÷ markup) — the amount we
            # advance the water mark by. Derived FROM the charge (not the other way
            # round) so rounding can never push the charge above the balance.
            coverable = round(charge / markup, 2)
            # Idempotency guard — the whole point of this loop being safe. The
            # scheduler can run in several workers at once (each container/worker has
            # its own APScheduler), so this sweep may execute N times concurrently.
            # CLAIM the slice before charging: this conditional update only matches if
            # the water mark is still exactly what we read, so of the N concurrent
            # sweeps exactly ONE wins and charges — the rest find the mark already
            # moved and skip. Without this, every concurrent copy charged the same
            # spend (the ×N over-charge bug).
            claimed = await db[collection].find_one_and_update(
                {"campaign_id": campaign_id, "spend_billed_ngn": prior},
                {"$set": {"spend_billed_ngn": round(billed + coverable, 2),
                          "last_billed_at": datetime.now(timezone.utc)}},
            )
            if claimed is None:
                continue   # another concurrent sweep already billed this slice
            try:
                await wallet.charge_ad_spend(
                    business_id, charge, campaign_id=campaign_id, meta_spend_ngn=coverable,
                )
                charged_total = round(charged_total + charge, 2)
            except InsufficientFundsError:
                # Balance dropped (e.g. another campaign on the same wallet billed first)
                # — release the claim so this slice is retried next sweep, then pause.
                await db[collection].update_one(
                    {"campaign_id": campaign_id}, {"$set": {"spend_billed_ngn": prior}},
                )
                ran_dry = True

        if not ran_dry:
            continue

        # Wallet can't cover the full slice → stop fronting money we can't recoup.
        # Claim the pause too (same concurrency guard) so it pauses + notifies once,
        # not once per concurrent sweep.
        first_to_pause = await db[collection].find_one_and_update(
            {"campaign_id": campaign_id, "paused_for_funds": {"$ne": True}},
            {"$set": {"paused_for_funds": True}},
        )
        if first_to_pause is None:
            continue

        if summary["delivery"] == "Active":
            try:
                await adapter.set_delivery(campaign_id, active=False)
                paused_total += 1
            except api_error_cls as e:
                print(f"[billing] pause failed for {campaign_id}: {e}", flush=True)

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

    return {"checked": checked, "charged_ngn": charged_total, "paused": paused_total}
