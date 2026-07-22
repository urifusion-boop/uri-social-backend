"""
Jane + Ads — real wallet funding via Squad (split-doc 1.4, the payment hookup).

Reuses the repo's existing Squad integration (`payment_service` for credential/mode
selection and the initiate/verify API shape) but keeps a SEPARATE flow and collection
(`jane_ads_topups`) so ad-wallet funding never touches the subscription/credit ledger.

Flow:
  initialize_topup → Squad checkout URL (customer pays in Naira)
  Squad webhook / verify → on success → WalletService.top_up (idempotent by reference)

The wallet crediting is the tested part; this module is the thin, Squad-specific glue.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.services.PaymentService import payment_service

from . import constants as C
from .store import MongoWalletStore
from .wallet import MinimumTopUpError, WalletService


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JaneAdsPayments:
    """Squad-backed top-ups for the Jane Ads custodial wallet."""

    def __init__(self, db) -> None:
        self._db = db
        self._topups = db.jane_ads_topups
        self._wallet = WalletService(MongoWalletStore(db))

    # ── Start a payment ─────────────────────────────────────────────────────────
    async def initialize_topup(self, business_id: str, amount_ngn: float, email: str) -> dict:
        """Create a Squad checkout for a wallet top-up. Returns the checkout URL the
        customer opens to pay. Nothing is credited until payment is confirmed."""
        if amount_ngn < C.MIN_TOPUP_NGN:
            raise MinimumTopUpError(
                f"Minimum top-up is ₦{C.MIN_TOPUP_NGN:,.0f}; got ₦{amount_ngn:,.0f}."
            )
        creds = await payment_service._get_squad_credentials()
        reference = f"JANEADS_{business_id[:8]}_{uuid.uuid4().hex[:12]}"

        await self._topups.insert_one({
            "reference": reference,
            "business_id": business_id,
            "amount_ngn": amount_ngn,
            "email": email,
            "status": "pending",
            "created_at": _now(),
        })

        web_app_url = getattr(__import__("app.core.config", fromlist=["settings"]).settings,
                              "WEB_APP_URL", "https://www.urisocial.com")
        payload = {
            "email": email,
            "amount": int(amount_ngn * 100),   # Squad expects Kobo
            "currency": "NGN",
            "initiate_type": "inline",
            "transaction_ref": reference,
            # Squad appends ?reference=<ref> to this on return; the campaigns page
            # reads it, calls verify, and refreshes the wallet. Landing on the
            # campaigns tab (not a standalone page) keeps the user in the same flow
            # they were funding the wallet for.
            "callback_url": f"{web_app_url}/workspace?tab=campaigns",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{creds['api_url']}/transaction/initiate",
                json=payload,
                headers={"Authorization": f"Bearer {creds['secret_key']}",
                         "Content-Type": "application/json"},
                timeout=30.0,
            )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            checkout_url = data["data"].get("checkout_url") or data["data"].get("authorization_url")
            return {"reference": reference, "amount_ngn": amount_ngn, "checkout_url": checkout_url}
        await self._topups.update_one({"reference": reference},
                                      {"$set": {"status": "failed", "squad_response": data}})
        raise RuntimeError(f"Squad initialize failed: {data.get('message')}")

    # ── Confirm a payment (verify + credit, idempotent) ─────────────────────────
    async def confirm_topup(self, reference: str) -> dict:
        """Verify the payment with Squad and, on success, credit the wallet exactly once."""
        rec = await self._topups.find_one({"reference": reference})
        if not rec:
            return {"status": "not_found"}
        if rec["status"] == "completed":
            return {"status": "completed", "already_credited": True}

        creds = await payment_service._get_squad_credentials()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{creds['api_url']}/transaction/verify/{reference}",
                headers={"Authorization": f"Bearer {creds['secret_key']}"},
                timeout=30.0,
            )
        data = resp.json()
        ok = (resp.status_code == 200 and data.get("success")
              and data.get("data", {}).get("transaction_status") == "success")
        if ok:
            return await self._credit(rec, data)
        await self._topups.update_one({"reference": reference},
                                      {"$set": {"status": "failed", "squad_response": data}})
        return {"status": "failed"}

    # ── Webhook (Squad → us) ────────────────────────────────────────────────────
    async def handle_webhook(self, payload: dict) -> dict:
        """Handle a Squad webhook for a Jane Ads top-up. Credits idempotently on success."""
        reference = payload.get("TransactionRef") or payload.get("transaction_ref")
        body = payload.get("Body", payload)
        status = str(body.get("transaction_status", "")).lower()
        if not reference:
            return {"status": "ignored", "reason": "no reference"}
        rec = await self._topups.find_one({"reference": reference})
        if not rec:
            return {"status": "ignored", "reason": "unknown reference"}  # not ours
        if rec["status"] == "completed":
            return {"status": "completed", "already_credited": True}
        if status == "success":
            return await self._credit(rec, payload)
        await self._topups.update_one({"reference": reference},
                                      {"$set": {"status": "failed", "squad_response": payload}})
        return {"status": "failed"}

    async def _credit(self, rec: dict, squad_response: dict) -> dict:
        """Credit the wallet (idempotent by reference) and mark the top-up completed."""
        txn = await self._wallet.top_up(
            rec["business_id"], rec["amount_ngn"], reference=rec["reference"]
        )
        await self._topups.update_one(
            {"reference": rec["reference"]},
            {"$set": {"status": "completed", "completed_at": _now(),
                      "squad_response": squad_response, "wallet_txn_id": txn.transaction_id}},
        )
        return {"status": "completed", "business_id": rec["business_id"],
                "amount_ngn": rec["amount_ngn"], "balance_ngn": txn.balance_after_ngn}
