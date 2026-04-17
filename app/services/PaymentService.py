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
import json
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
from app.services.TrialService import trial_service
from app.services.CreditService import credit_service
from app.core.config import settings


class PaymentService:
    """
    Payment processing via SQUAD gateway
    PRD Section 6: Billing System Requirements
    """

    def __init__(self):
        self._db: Optional[AsyncIOMotorDatabase] = None

        # Store all credentials (we'll select dynamically based on mode)
        self.sandbox_secret_key = getattr(settings, 'SQUAD_SANDBOX_SECRET_KEY', '')
        self.sandbox_public_key = getattr(settings, 'SQUAD_SANDBOX_PUBLIC_KEY', '')
        self.live_secret_key = getattr(settings, 'SQUAD_LIVE_SECRET_KEY', '')
        self.live_public_key = getattr(settings, 'SQUAD_LIVE_PUBLIC_KEY', '')

        # Default mode from environment (fallback)
        self.default_mode = getattr(settings, 'SQUAD_MODE', 'sandbox').lower()

        print(f"🔐 SQUAD Payment Gateway initialized (default: {self.default_mode.upper()})")

        # User-facing callback URL (where users return after payment)
        web_app_url = getattr(settings, 'WEB_APP_URL', 'https://www.urisocial.com')
        self.callback_url = f'{web_app_url}/dashboard/billing'

    async def _get_current_mode(self) -> str:
        """Get current Squad mode from config (production: always use config value)"""
        # Production: Always use config.py value, no database override
        return self.default_mode

    async def _get_squad_credentials(self):
        """Get Squad credentials based on current mode"""
        mode = await self._get_current_mode()

        if mode == 'live':
            return {
                'secret_key': self.live_secret_key,
                'public_key': self.live_public_key,
                'api_url': 'https://api-d.squadco.com',
                'mode': 'live'
            }
        else:
            return {
                'secret_key': self.sandbox_secret_key,
                'public_key': self.sandbox_public_key,
                'api_url': 'https://sandbox-api-d.squadco.com',
                'mode': 'sandbox'
            }

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
        user_email: str,
        test_amount: int = None,
        test_credits: int = None
    ) -> InitializePaymentResponse:
        """
        Initialize SQUAD payment checkout
        PRD 6.3: Payment Flow
        1. User selects plan
        2. Payment processed
        3. On success: Assign credits, Activate subscription

        Special handling for tier_id='test': Use custom test_amount and test_credits
        """
        # Handle test tier with custom amounts
        if tier_id == 'test':
            print(f"🧪 Test payment requested: amount={test_amount}, credits={test_credits}")
            if not test_amount or not test_credits:
                raise ValueError(f"test_amount and test_credits required for test tier (received: amount={test_amount}, credits={test_credits})")

            # Create a temporary tier object for test payment
            from app.domain.models.billing_models import SubscriptionTier
            tier = SubscriptionTier(
                tier_id='test',
                name='Test Plan',
                price_ngn=test_amount,
                credits=test_credits,
                price_per_credit=test_amount / test_credits,
                features=['Test payment'],
                is_active=True
            )
        else:
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

        # Get current Squad credentials (dynamic mode switching)
        creds = await self._get_squad_credentials()
        print(f"💳 Initializing payment in {creds['mode'].upper()} mode")

        # Initialize SQUAD payment
        try:
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {creds['secret_key']}",
                    "Content-Type": "application/json"
                }

                # SQUAD API payload structure (per official docs)
                # Note: Sandbox doesn't accept "meta" field, only live does
                # IMPORTANT: SQUAD expects amount in KOBO (smallest unit), not Naira
                # 1 Naira = 100 Kobo, so ₦15,000 = 1,500,000 kobo
                payload = {
                    "email": user_email,
                    "amount": tier.price_ngn * 100,  # Convert Naira to Kobo (multiply by 100)
                    "currency": "NGN",
                    "initiate_type": "inline",  # Required: opens payment modal
                    "transaction_ref": transaction_ref,
                    "callback_url": self.callback_url
                }

                response = await client.post(
                    f"{creds['api_url']}/transaction/initiate",
                    json=payload,
                    headers=headers,
                    timeout=30.0
                )

                response_data = response.json()

                # SQUAD response structure: { "status": 200, "success": true, "message": "", "data": { "checkout_url": "..." } }
                if response.status_code == 200 and response_data.get("success"):
                    # Extract checkout URL from SQUAD response
                    data = response_data.get("data", {})
                    checkout_url = data.get("checkout_url") or data.get("authorization_url")  # Some gateways use authorization_url

                    if not checkout_url:
                        raise Exception(f"SQUAD response missing checkout_url: {response_data}")

                    return InitializePaymentResponse(
                        payment_url=checkout_url,
                        transaction_ref=transaction_ref,
                        amount=tier.price_ngn,
                        email=user_email,
                        currency="NGN",
                        public_key=creds['public_key']
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

        # Get current Squad credentials (dynamic mode switching)
        creds = await self._get_squad_credentials()

        # Verify with SQUAD
        try:
            async with httpx.AsyncClient() as client:
                headers = {
                    "Authorization": f"Bearer {creds['secret_key']}"
                }

                response = await client.get(
                    f"{creds['api_url']}/transaction/verify/{transaction_ref}",
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

        IMPORTANT: Preserves trial credits as bonus credits when trial user subscribes
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

        # Check if user has remaining trial credits - preserve them as bonus credits
        trial_status = await trial_service.get_trial_status(user_id)
        remaining_trial_credits = 0

        if trial_status and trial_status.trial_active and trial_status.credits_remaining > 0:
            remaining_trial_credits = trial_status.credits_remaining
            print(f"💎 Preserving {remaining_trial_credits} trial credits as bonus for user {user_id}")

        # Activate subscription (this allocates subscription credits)
        await subscription_service.create_subscription(
            user_id=user_id,
            tier_id=tier_id
        )

        # Add trial credits as bonus credits after subscription is created
        if remaining_trial_credits > 0:
            await credit_service.add_bonus_credits(
                user_id=user_id,
                bonus_amount=remaining_trial_credits,
                reason="trial_credits_preserved"
            )
            print(f"✅ Added {remaining_trial_credits} bonus credits from trial")

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

        Webhook structure (per SQUAD docs):
        {
            "Event": "charge_successful",
            "TransactionRef": "4678388588A0",
            "Body": {
                "transaction_ref": "4678388588A0",
                "transaction_status": "success" or "Success",
                "amount": 83000,
                "email": "user@example.com",
                ...
            }
        }
        """
        # Verify webhook signature (SQUAD HMAC-SHA512 via x-squad-encrypted-body header)
        if signature and self.squad_secret_key:
            if not self._verify_webhook_signature(payload, signature):
                raise ValueError("Invalid webhook signature")

        # SQUAD webhook structure: Event + TransactionRef + Body
        transaction_ref = payload.get("TransactionRef") or payload.get("transaction_ref")
        body = payload.get("Body", {})
        transaction_status = body.get("transaction_status", "").lower()  # Normalize to lowercase

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

        Per SQUAD docs:
        - Header: x-squad-encrypted-body
        - Algorithm: HMAC SHA512
        - Key: Your secret key
        - Payload: JSON string of the webhook body
        """
        # Serialize payload to JSON string (SQUAD uses JSON.stringify equivalent)
        payload_string = json.dumps(payload, separators=(',', ':'), sort_keys=False)

        # Create HMAC-SHA512 hash
        expected_signature = hmac.new(
            self.squad_secret_key.encode('utf-8'),
            payload_string.encode('utf-8'),
            hashlib.sha512
        ).hexdigest().upper()  # SQUAD sends signature in UPPERCASE

        # Use timing-safe comparison
        return hmac.compare_digest(expected_signature, signature.upper())

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
