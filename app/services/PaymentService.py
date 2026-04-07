"""
Payment Gateway Service - SQUAD Integration
Strictly aligned with PRICING PRD V1

Handles payment processing:
- SQUAD payment initialization (PRD 6.3)
- Payment verification (PRD 6.4)
- Webhook handling (PRD 6.3)
- Failure handling (PRD 6.4)
"""
import httpx
import hashlib
import hmac
from typing import Optional, Dict
from datetime import datetime
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.domain.models.billing_models import (
    PaymentTransaction,
    InitializePaymentResponse
)
from app.services.SubscriptionService import subscription_service
from app.core.config import settings


class PaymentService:
    """
    Payment processing via SQUAD gateway
    PRD Section 6: Billing System Requirements
    """

    def __init__(self):
        self._db: Optional[AsyncIOMotorDatabase] = None

        # SQUAD API configuration
        self.squad_secret_key = getattr(settings, 'SQUAD_SECRET_KEY', '')
        self.squad_public_key = getattr(settings, 'SQUAD_PUBLIC_KEY', '')
        self.squad_webhook_secret = getattr(settings, 'SQUAD_WEBHOOK_SECRET', '')
        self.squad_api_url = "https://api.squadco.com"
        self.callback_url = getattr(settings, 'SQUAD_CALLBACK_URL', 'https://www.urisocial.com/checkout/callback')

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            self._db = get_db()
        return self._db

    @property
    def payment_transactions_collection(self):
        return self.db["payment_transactions"]

    # ==================== PRD 6.3: Payment Flow ====================

    async def initialize_payment(
        self,
        user_id: str,
        tier_id: str,
        user_email: str
    ) -> InitializePaymentResponse:
        """
        Initialize SQUAD payment checkout
        PRD 6.3: Payment Flow
        1. User selects plan
        2. Payment processed
        3. On success: Assign credits, Activate subscription
        """
        # Validate tier
        validation = await subscription_service.validate_tier_purchase(user_id, tier_id)
        if not validation["valid"]:
            raise ValueError(validation["message"])

        tier = validation["tier"]

        # Generate unique transaction reference
        transaction_ref = f"URI_{user_id[:8]}_{tier_id.upper()}_{int(datetime.utcnow().timestamp())}"

        # Create pending payment transaction
        payment = PaymentTransaction(
            user_id=user_id,
            transaction_ref=transaction_ref,
            amount=tier.price_ngn,
            currency="NGN",
            status="pending",
            gateway="squad",
            subscription_tier=tier_id,
            created_at=datetime.utcnow()
        )

        await self.payment_transactions_collection.insert_one(
            payment.dict(exclude_none=True)
        )

        # Initialize SQUAD payment
        try:
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {self.squad_secret_key}",
                    "Content-Type": "application/json"
                }

                payload = {
                    "email": user_email,
                    "amount": tier.price_ngn * 100,  # SQUAD expects amount in kobo (₦1 = 100 kobo)
                    "currency": "NGN",
                    "transaction_ref": transaction_ref,
                    "callback_url": self.callback_url,
                    "metadata": {
                        "user_id": user_id,
                        "tier_id": tier_id,
                        "tier_name": tier.name
                    }
                }

                response = await client.post(
                    f"{self.squad_api_url}/transaction/initiate",
                    json=payload,
                    headers=headers,
                    timeout=30.0
                )

                response_data = response.json()

                if response.status_code == 200 and response_data.get("success"):
                    # Extract checkout URL from SQUAD response
                    checkout_url = response_data.get("data", {}).get("checkout_url")

                    return InitializePaymentResponse(
                        payment_url=checkout_url,
                        transaction_ref=transaction_ref,
                        amount=tier.price_ngn
                    )
                else:
                    # PRD 6.4: Failure Handling
                    await self._mark_payment_failed(transaction_ref, response_data)
                    raise Exception(f"SQUAD initialization failed: {response_data.get('message')}")

        except httpx.RequestError as e:
            await self._mark_payment_failed(transaction_ref, {"error": str(e)})
            raise Exception(f"Payment gateway connection failed: {str(e)}")

    # ==================== Payment Verification ====================

    async def verify_payment(self, transaction_ref: str) -> bool:
        """
        Verify payment status with SQUAD
        PRD 6.3: On success: Assign credits, Activate subscription
        """
        # Get payment transaction
        payment_doc = await self.payment_transactions_collection.find_one(
            {"transaction_ref": transaction_ref}
        )

        if not payment_doc:
            return False

        # If already completed, return true
        if payment_doc["status"] == "completed":
            return True

        # Verify with SQUAD
        try:
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {self.squad_secret_key}"
                }

                response = await client.get(
                    f"{self.squad_api_url}/transaction/verify/{transaction_ref}",
                    headers=headers,
                    timeout=30.0
                )

                response_data = response.json()

                if response.status_code == 200 and response_data.get("success"):
                    transaction_data = response_data.get("data", {})
                    transaction_status = transaction_data.get("transaction_status")

                    if transaction_status == "success":
                        # Payment successful - activate subscription
                        await self._complete_payment(
                            transaction_ref=transaction_ref,
                            user_id=payment_doc["user_id"],
                            tier_id=payment_doc["subscription_tier"],
                            squad_response=response_data
                        )
                        return True
                    elif transaction_status in ["failed", "cancelled"]:
                        await self._mark_payment_failed(transaction_ref, response_data)
                        return False

        except httpx.RequestError as e:
            print(f"Payment verification error: {str(e)}")
            return False

        return False

    async def _complete_payment(
        self,
        transaction_ref: str,
        user_id: str,
        tier_id: str,
        squad_response: Dict
    ) -> None:
        """
        Complete payment and activate subscription
        PRD 6.3: On success: Assign credits, Activate subscription
        """
        # Update payment status
        await self.payment_transactions_collection.update_one(
            {"transaction_ref": transaction_ref},
            {
                "$set": {
                    "status": "completed",
                    "completed_at": datetime.utcnow(),
                    "squad_response": squad_response
                }
            }
        )

        # Activate subscription (this allocates credits)
        await subscription_service.create_subscription(
            user_id=user_id,
            tier_id=tier_id
        )

    async def _mark_payment_failed(
        self,
        transaction_ref: str,
        error_data: Dict
    ) -> None:
        """
        Mark payment as failed
        PRD 6.4: Failure Handling - Do not assign credits, Show payment error, Allow retry
        """
        await self.payment_transactions_collection.update_one(
            {"transaction_ref": transaction_ref},
            {
                "$set": {
                    "status": "failed",
                    "squad_response": error_data,
                    "completed_at": datetime.utcnow()
                }
            }
        )

    # ==================== Webhook Handler ====================

    async def handle_webhook(
        self,
        payload: Dict,
        signature: Optional[str] = None
    ) -> bool:
        """
        Handle SQUAD webhook callback
        PRD 6.3: SQUAD sends webhook to POST /billing/webhook
        """
        # Verify webhook signature (SQUAD HMAC-SHA512)
        if signature and self.squad_webhook_secret:
            if not self._verify_webhook_signature(payload, signature):
                raise ValueError("Invalid webhook signature")

        # Extract transaction details
        transaction_ref = payload.get("transaction_ref")
        transaction_status = payload.get("transaction_status")

        if not transaction_ref:
            raise ValueError("Missing transaction_ref in webhook payload")

        # Get payment record
        payment_doc = await self.payment_transactions_collection.find_one(
            {"transaction_ref": transaction_ref}
        )

        if not payment_doc:
            raise ValueError(f"Payment transaction not found: {transaction_ref}")

        # Handle based on status
        if transaction_status == "success":
            await self._complete_payment(
                transaction_ref=transaction_ref,
                user_id=payment_doc["user_id"],
                tier_id=payment_doc["subscription_tier"],
                squad_response=payload
            )
            return True
        elif transaction_status in ["failed", "cancelled"]:
            await self._mark_payment_failed(transaction_ref, payload)
            return False

        return False

    def _verify_webhook_signature(self, payload: Dict, signature: str) -> bool:
        """
        Verify SQUAD webhook signature using HMAC-SHA512
        """
        # Create HMAC hash of payload
        payload_string = str(payload)
        expected_signature = hmac.new(
            self.squad_webhook_secret.encode(),
            payload_string.encode(),
            hashlib.sha512
        ).hexdigest()

        return hmac.compare_digest(expected_signature, signature)

    # ==================== Transaction Retrieval ====================

    async def get_payment_transaction(
        self,
        transaction_ref: str
    ) -> Optional[PaymentTransaction]:
        """Get payment transaction by reference"""
        payment_doc = await self.payment_transactions_collection.find_one(
            {"transaction_ref": transaction_ref}
        )

        if not payment_doc:
            return None

        payment_doc["_id"] = str(payment_doc["_id"])
        return PaymentTransaction(**payment_doc)

    async def get_user_payment_history(
        self,
        user_id: str,
        limit: int = 20
    ) -> list[Dict]:
        """Get user's payment history"""
        cursor = self.payment_transactions_collection.find(
            {"user_id": user_id}
        ).sort("created_at", -1).limit(limit)

        payments = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            payments.append(doc)

        return payments


# Singleton instance
payment_service = PaymentService()
