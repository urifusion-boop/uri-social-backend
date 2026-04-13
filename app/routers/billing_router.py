"""
Billing & Credit System API Router
Strictly aligned with PRICING PRD V1

Endpoints:
- POST /billing/initialize-payment - Start SQUAD checkout (PRD 6.3)
- POST /billing/verify-payment - Verify transaction (PRD 6.3)
- POST /billing/webhook - SQUAD callback (PRD 6.3)
- GET /billing/credits/balance - Get current balance (PRD 7.1)
- GET /billing/credits/transactions - Transaction history (PRD 9)
- GET /billing/subscription/current - Current subscription (PRD 6.1)
- GET /billing/subscription/tiers - Available plans (PRD 5)
- POST /billing/subscription/cancel - Cancel subscription (PRD 13)
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from typing import Optional, List
from app.core.auth_bearer import JWTBearer
from app.domain.models.billing_models import (
    InitializePaymentRequest,
    InitializePaymentResponse,
    VerifyPaymentRequest,
    CreditBalanceResponse,
    SubscriptionResponse,
    SubscriptionTier
)
from app.services.CreditService import credit_service
from app.services.SubscriptionService import subscription_service
from app.services.PaymentService import payment_service
from app.services.TrialService import trial_service
from app.domain.models.billing_models import TrialStatusResponse

router = APIRouter(prefix="/billing", tags=["Billing"])


# ==================== HELPER: Extract User ID from JWT ====================

def get_user_id(jwt_payload: dict = Depends(JWTBearer())) -> str:
    """Extract user_id from JWT payload"""
    claims = jwt_payload.get("claims", {})
    user_id = claims.get("userId")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: user_id not found")
    return user_id


def get_user_email(jwt_payload: dict = Depends(JWTBearer())) -> str:
    """Extract email from JWT payload"""
    claims = jwt_payload.get("claims", {})
    email = claims.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token: email not found")
    return email


# ==================== PRD 6.3: Payment Flow ====================

@router.post("/initialize-payment", response_model=InitializePaymentResponse)
async def initialize_payment(
    body: InitializePaymentRequest,
    user_id: str = Depends(get_user_id),
    user_email: str = Depends(get_user_email)
):
    """
    Initialize SQUAD payment checkout
    PRD 6.3: Payment Flow
    1. User selects plan
    2. Payment processed via SQUAD
    3. Returns checkout URL
    """
    try:
        result = await payment_service.initialize_payment(
            user_id=user_id,
            tier_id=body.tier_id,
            user_email=user_email
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Payment initialization failed: {str(e)}")


@router.post("/verify-payment")
async def verify_payment(
    body: VerifyPaymentRequest,
    user_id: str = Depends(get_user_id)
):
    """
    Verify payment status with SQUAD
    PRD 6.3: On success: Assign credits, Activate subscription
    Frontend polls this after redirect from SQUAD
    """
    try:
        is_verified = await payment_service.verify_payment(body.transaction_ref)

        if is_verified:
            return {
                "status": True,
                "responseCode": 200,
                "responseMessage": "Payment verified successfully",
                "responseData": {
                    "verified": True,
                    "transaction_ref": body.transaction_ref
                }
            }
        else:
            return {
                "status": False,
                "responseCode": 400,
                "responseMessage": "Payment verification failed or pending",
                "responseData": {
                    "verified": False,
                    "transaction_ref": body.transaction_ref
                }
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")


@router.post("/webhook")
async def squad_webhook(
    request: Request,
    x_squad_signature: Optional[str] = Header(None)
):
    """
    SQUAD webhook callback
    PRD 6.3: SQUAD sends webhook to verify payment
    This endpoint should be publicly accessible (no JWT required)
    """
    try:
        payload = await request.json()

        # Handle webhook
        success = await payment_service.handle_webhook(
            payload=payload,
            signature=x_squad_signature
        )

        if success:
            return {
                "status": True,
                "responseCode": 200,
                "responseMessage": "Webhook processed successfully"
            }
        else:
            return {
                "status": False,
                "responseCode": 400,
                "responseMessage": "Webhook processing failed"
            }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Webhook error: {str(e)}")


# ==================== PRD 7.1: User Wallet / Credit Balance ====================

@router.get("/credits/balance", response_model=CreditBalanceResponse)
async def get_credit_balance(user_id: str = Depends(get_user_id)):
    """
    Get user's current credit balance
    PRD 7.1: User Wallet (total_credits, credits_used, credits_remaining)
    PRD 7.3: Low Credit Warning when credits ≤ 3
    """
    try:
        balance = await credit_service.get_credit_balance(user_id)
        return balance
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get balance: {str(e)}")


@router.get("/credits/transactions")
async def get_credit_transactions(
    limit: int = 50,
    user_id: str = Depends(get_user_id)
):
    """
    Get user's credit transaction history
    PRD 11: Must log all credit usage events
    """
    try:
        transactions = await credit_service.get_transaction_history(user_id, limit)
        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": "Transaction history retrieved",
            "responseData": transactions
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get transactions: {str(e)}")


# ==================== PRD 6.1: Subscription Management ====================

@router.get("/subscription/current", response_model=SubscriptionResponse)
async def get_current_subscription(user_id: str = Depends(get_user_id)):
    """
    Get user's current active subscription
    PRD 6.1: Subscription details
    """
    try:
        subscription = await subscription_service.get_current_subscription(user_id)

        if not subscription:
            raise HTTPException(
                status_code=404,
                detail="No active subscription found"
            )

        return subscription
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get subscription: {str(e)}")


@router.get("/subscription/tiers", response_model=List[SubscriptionTier])
async def get_subscription_tiers():
    """
    Get all available subscription tiers
    PRD Section 5: Plan Structure
    - Starter: ₦15,000 / 20 credits
    - Growth: ₦25,000 / 35 credits
    - Pro: ₦40,000 / 50 credits
    - Agency: ₦80,000 / 100 credits
    - Custom: ₦750 per credit
    """
    try:
        tiers = await subscription_service.get_all_tiers(active_only=True)
        return tiers
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get tiers: {str(e)}")


@router.post("/subscription/cancel")
async def cancel_subscription(user_id: str = Depends(get_user_id)):
    """
    Cancel user's subscription
    PRD 13: MVP Scope allows cancellation
    Credits remain until end of billing cycle
    """
    try:
        success = await subscription_service.cancel_subscription(user_id)

        if success:
            return {
                "status": True,
                "responseCode": 200,
                "responseMessage": "Subscription cancelled successfully",
                "responseData": {
                    "cancelled": True,
                    "note": "Your remaining credits will be available until the end of your billing cycle"
                }
            }
        else:
            raise HTTPException(status_code=404, detail="No active subscription found")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel subscription: {str(e)}")


# ==================== Payment History ====================

@router.get("/payments/history")
async def get_payment_history(
    limit: int = 20,
    user_id: str = Depends(get_user_id)
):
    """
    Get user's payment transaction history
    Shows all payment attempts (completed, pending, failed)
    """
    try:
        payments = await payment_service.get_user_payment_history(user_id, limit)
        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": "Payment history retrieved",
            "responseData": payments
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get payment history: {str(e)}")


# ==================== PRD 8: Credit Exhaustion Check ====================

@router.get("/credits/can-generate")
async def can_generate_content(user_id: str = Depends(get_user_id)):
    """
    Check if user can generate content (has credits)
    PRD 8: When credits = 0, block new campaign generation
    """
    try:
        is_blocked = await credit_service.is_blocked(user_id)
        has_credits = await credit_service.check_sufficient_credits(user_id)

        if is_blocked or not has_credits:
            return {
                "status": False,
                "responseCode": 402,
                "responseMessage": "You've run out of credits. Upgrade to continue.",
                "responseData": {
                    "can_generate": False,
                    "blocked": True
                }
            }
        else:
            balance = await credit_service.get_credit_balance(user_id)
            return {
                "status": True,
                "responseCode": 200,
                "responseMessage": "You can generate content",
                "responseData": {
                    "can_generate": True,
                    "blocked": False,
                    "credits_remaining": balance.credits_remaining
                }
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check status: {str(e)}")


# ==================== Squad Mode Management (Admin) ====================

@router.get("/squad/mode")
async def get_squad_mode(user_id: str = Depends(get_user_id)):
    """
    Get current Squad payment mode (sandbox or live)
    Returns the active mode and available credentials
    """
    try:
        from app.core.config import settings
        from app.database import get_db

        db = get_db()

        # Check database override first
        settings_doc = await db["app_settings"].find_one({"setting": "squad_mode"})
        if settings_doc and settings_doc.get("value") in ["sandbox", "live"]:
            current_mode = settings_doc["value"]
        else:
            current_mode = getattr(settings, 'SQUAD_MODE', 'sandbox').lower()

        has_sandbox = bool(getattr(settings, 'SQUAD_SANDBOX_SECRET_KEY', None))
        has_live = bool(getattr(settings, 'SQUAD_LIVE_SECRET_KEY', None))

        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": "Squad mode retrieved",
            "responseData": {
                "current_mode": current_mode,
                "available_modes": {
                    "sandbox": has_sandbox,
                    "live": has_live
                }
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get Squad mode: {str(e)}")


@router.post("/squad/mode")
async def set_squad_mode(
    request: Request,
    user_id: str = Depends(get_user_id)
):
    """
    Switch Squad payment mode (sandbox or live)
    NO SERVER RESTART REQUIRED - changes take effect immediately!
    Saves mode to database and all future payments will use the new mode.
    """
    try:
        from app.core.config import settings
        from app.database import get_db
        from datetime import datetime

        db = get_db()
        body = await request.json()
        new_mode = body.get("mode", "").lower()

        if new_mode not in ["sandbox", "live"]:
            raise HTTPException(
                status_code=400,
                detail="Invalid mode. Must be 'sandbox' or 'live'"
            )

        # Save mode to database (instant switch!)
        await db["app_settings"].update_one(
            {"setting": "squad_mode"},
            {
                "$set": {
                    "setting": "squad_mode",
                    "value": new_mode,
                    "updated_at": datetime.utcnow(),
                    "updated_by": user_id
                }
            },
            upsert=True
        )

        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": f"✅ Squad mode switched to {new_mode.upper()} successfully! Changes take effect immediately.",
            "responseData": {
                "new_mode": new_mode,
                "requires_restart": False,
                "instant_switch": True,
                "warning": "LIVE MODE will process real payments!" if new_mode == "live" else "SANDBOX MODE is for testing only"
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set Squad mode: {str(e)}")


# ==================== Free Trial System (PRD V1) ====================

@router.get("/trial/status")
async def get_trial_status(user_id: str = Depends(get_user_id)):
    """
    Get user's free trial status
    PRD 5.3: Expiry check on login, content generation
    PRD 4.2: trial_active = current_time < trial_end_date AND credits_remaining > 0
    """
    try:
        status = await trial_service.get_trial_status(user_id)
        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": "Trial status retrieved",
            "responseData": status.dict(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get trial status: {str(e)}")


@router.post("/trial/activate")
async def activate_trial(user_id: str = Depends(get_user_id)):
    """
    Manually activate free trial (if not auto-activated on signup)
    PRD 8: One trial per user — prevents duplicate activation
    """
    try:
        has_used = await trial_service.has_used_trial(user_id)
        if has_used:
            status = await trial_service.get_trial_status(user_id)
            return {
                "status": True,
                "responseCode": 200,
                "responseMessage": "Trial already used",
                "responseData": status.dict(),
            }

        status = await trial_service.activate_trial(user_id)
        return {
            "status": True,
            "responseCode": 201,
            "responseMessage": "Trial activated successfully",
            "responseData": status.dict(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to activate trial: {str(e)}")


@router.get("/trial/can-generate")
async def trial_can_generate(user_id: str = Depends(get_user_id)):
    """
    Check if trial user can generate content
    PRD 5.4: If trial inactive → block content/image generation
    """
    try:
        can = await trial_service.can_generate(user_id)
        status = await trial_service.get_trial_status(user_id)

        if not can:
            message = "Your free trial has ended. Upgrade to continue creating content."
            if status.trial_expired:
                message = "Your free trial has ended. Upgrade to keep creating."
            elif status.credits_remaining == 0:
                message = "You've used all your trial credits. Upgrade to continue."

            return {
                "status": False,
                "responseCode": 402,
                "responseMessage": message,
                "responseData": {
                    "can_generate": False,
                    "trial_expired": status.trial_expired,
                    "credits_remaining": status.credits_remaining,
                    "days_remaining": status.days_remaining,
                }
            }

        return {
            "status": True,
            "responseCode": 200,
            "responseMessage": "Trial active — you can generate content",
            "responseData": {
                "can_generate": True,
                "credits_remaining": status.credits_remaining,
                "days_remaining": status.days_remaining,
                "low_credit_warning": status.low_credit_warning,
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check trial: {str(e)}")
