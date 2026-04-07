# 💳 URI Social - Credit-Based Billing System

**Strictly aligned with PRICING PRD V1**

## Overview

Complete credit-based billing system with SQUAD payment integration. Users purchase monthly subscription tiers that provide campaign credits.

### Core Principle (PRD)
> **1 Credit = 1 Complete Content Campaign**
> - AI-generated image
> - Caption generation
> - Multi-platform formatting (Instagram, X, LinkedIn)

---

## 📋 Implementation Status

### ✅ Completed Backend Features

- [x] **Database Models** (`app/domain/models/billing_models.py`)
  - UserCreditWallet (PRD 7.1: total_credits, credits_used, credits_remaining)
  - CreditTransaction (PRD 11: Audit logging)
  - SubscriptionTier (PRD 5: Plan definitions)
  - PaymentTransaction (PRD 6: Payment tracking)
  - CampaignTracking (PRD 9: retry_count + image_retry_count)

- [x] **Services** (`app/services/`)
  - `CreditService.py` - Credit balance & deduction logic (PRD 7 & 8)
  - `SubscriptionService.py` - Plan management (PRD 5 & 6.1)
  - `PaymentService.py` - SQUAD payment gateway integration (PRD 6.2)

- [x] **API Endpoints** (`app/routers/billing_router.py`)
  - `POST /billing/initialize-payment` - Start SQUAD checkout
  - `POST /billing/verify-payment` - Verify transaction
  - `POST /billing/webhook` - SQUAD callback handler
  - `GET /billing/credits/balance` - Get credit balance (with low credit warning)
  - `GET /billing/credits/transactions` - Transaction history
  - `GET /billing/subscription/current` - Current subscription
  - `GET /billing/subscription/tiers` - Available plans
  - `POST /billing/subscription/cancel` - Cancel subscription
  - `GET /billing/payments/history` - Payment history
  - `GET /billing/credits/can-generate` - Check if user can generate

- [x] **Credit Enforcement**
  - Generation endpoint blocks if credits = 0 (PRD 8)
  - Deducts 1 credit after successful campaign generation (PRD 3.1)
  - Regenerate endpoint tracks retry_count (PRD 3.2)
  - First retry FREE, second retry costs 1 credit (PRD 3.2)

- [x] **Campaign Tracking** (PRD 9)
  - `retry_count` field in content_requests
  - `image_retry_count` field in content_requests
  - `text_edit_count` field in content_requests (unlimited, no cost per PRD 4.1)
  - `credits_used` field per campaign

- [x] **Database Initialization**
  - Startup event seeds 5 subscription tiers (PRD 5)
  - Index creation script for performance optimization

---

## 💰 Subscription Tiers (PRD Section 5)

| Tier | Price | Credits | Price/Credit | Features |
|------|-------|---------|--------------|----------|
| **Starter** | ₦15,000 | 20 | ₦750 | Basic features, Email support |
| **Growth** | ₦25,000 | 35 | ₦714 | + Priority support, Analytics |
| **Pro** | ₦40,000 | 50 | ₦800 | + Team collaboration |
| **Agency** | ₦80,000 | 100 | ₦800 | + White label, Dedicated support |
| **Custom** | ₦750/credit | Pay-per-use | ₦750 | No monthly commitment |

**Credit Reset:** Monthly (PRD 5.2) - Credits reset every billing cycle, no rollover unless top-up (excluded from MVP)

---

## 🔄 Credit Usage Logic (PRD Section 3)

### Campaign Generation
- **First generation**: 1 credit deducted (PRD 3.1)
- **First retry**: FREE (PRD 3.2)
- **Second retry**: 1 credit deducted (PRD 3.2)
- **Third+ retry**: 1 credit each

### Text vs Image Rules (PRD Section 4)
| Action | Credit Cost |
|--------|-------------|
| Text rewrites (caption edits, tone changes) | **FREE (unlimited)** |
| Initial image generation | **Included in 1st credit** |
| First image retry | **FREE** |
| Second image retry | **1 credit** |

### System Behavior (PRD 3.3)
> Before second retry, system must show:
> **"This action will use 1 credit. Continue?"**

---

## 🔒 Credit Enforcement

### Generation Endpoint
```python
# /social-media/generate-content
# PRD 7.2 & 8: Credit check before generation

has_credits = await credit_service.check_sufficient_credits(user_id)
if not has_credits:
    raise HTTPException(
        status_code=402,
        detail="You've run out of credits. Upgrade to continue."  # PRD 8
    )

# Generate content...

# PRD 7.2: Deduct after successful generation
await credit_service.deduct_credit(user_id, campaign_id, reason="campaign_generation")
```

### Regenerate Endpoint
```python
# /social-media/regenerate-content/{draft_id}
# PRD 3.2: Retry Rules

current_retry_count = request.get("retry_count", 0)
new_retry_count = current_retry_count + 1

# First retry FREE, second+ retry costs credit
if new_retry_count >= 2:
    has_credits = await credit_service.check_sufficient_credits(user_id)
    if not has_credits:
        return error("Insufficient credits for retry")

    await credit_service.deduct_credit(user_id, campaign_id, reason="retry")
```

---

## 💳 SQUAD Payment Integration (PRD 6.2)

### Environment Variables
```bash
# Add to .env
SQUAD_SECRET_KEY=sq_secret_xxxxxxxxxxxxx
SQUAD_PUBLIC_KEY=sq_public_xxxxxxxxxxxxx
SQUAD_WEBHOOK_SECRET=webhook_secret_xxxxx
SQUAD_CALLBACK_URL=https://www.urisocial.com/checkout/callback
```

### Payment Flow (PRD 6.3)

1. **User selects plan** on frontend `/pricing` page
2. **Frontend calls** `POST /billing/initialize-payment` with `tier_id`
3. **Backend**:
   - Creates pending transaction in `payment_transactions`
   - Calls SQUAD API to initialize payment
   - Returns SQUAD checkout URL
4. **User completes payment** on SQUAD hosted page
5. **SQUAD sends webhook** to `POST /billing/webhook`
6. **Backend webhook handler**:
   - Verifies transaction with SQUAD
   - Updates payment status to "completed"
   - Calls `SubscriptionService.create_subscription()`
   - Calls `CreditService.allocate_credits()`
7. **SQUAD redirects** user back to callback URL
8. **Frontend polls** `GET /billing/verify-payment?ref=xxx`
9. **Show success** and redirect to `/workspace`

### Failure Handling (PRD 6.4)
- Do NOT assign credits on payment failure
- Show payment error
- Allow retry

---

## 📊 Database Collections

### user_credits
```javascript
{
  user_id: "507f1f77bcf86cd799439011",
  total_credits: 35,           // PRD 7.1: Total allocated this cycle
  credits_used: 15,            // PRD 7.1: Credits consumed
  credits_remaining: 20,       // PRD 7.1: Calculated (total - used)
  subscription_tier: "growth",
  next_renewal: ISODate("2026-05-06"),
  created_at: ISODate,
  updated_at: ISODate
}
```

### credit_transactions
```javascript
{
  user_id: "507f1f77bcf86cd799439011",
  type: "deduction",  // allocation|deduction|bonus|refund
  amount: -1,
  balance_before: 21,
  balance_after: 20,
  reason: "campaign_generation",  // subscription|retry|campaign_generation
  campaign_id: "507f191e810c19729de860ea",
  retry_count: 0,
  created_at: ISODate
}
```

### subscription_tiers
```javascript
{
  tier_id: "growth",
  name: "Growth Plan",
  price_ngn: 25000,
  credits: 35,
  price_per_credit: 714,
  features: ["35 Campaigns/Month", "Priority Support"],
  is_active: true,
  created_at: ISODate
}
```

### payment_transactions
```javascript
{
  user_id: "507f1f77bcf86cd799439011",
  transaction_ref: "SQUAD_123456789",
  amount: 25000,
  currency: "NGN",
  status: "completed",  // pending|completed|failed
  payment_method: "card",
  gateway: "squad",
  subscription_tier: "growth",
  squad_response: { /* full SQUAD webhook data */ },
  created_at: ISODate,
  completed_at: ISODate
}
```

### content_requests (PRD 9: Campaign Tracking)
```javascript
{
  id: "507f191e810c19729de860ea",
  user_id: "507f1f77bcf86cd799439011",
  credits_used: 1,
  retry_count: 0,              // Full campaign retries (PRD 3.2)
  image_retry_count: 0,        // Image-only retries (PRD 4.2)
  text_edit_count: 5,          // Text rewrites (unlimited, no cost)
  status: "completed",
  created_at: ISODate
}
```

---

## 🚀 Setup & Deployment

### 1. Install Dependencies
```bash
cd uri-social-backend
# Dependencies already in requirements.txt:
# - motor (MongoDB async driver)
# - httpx (for SQUAD API calls)
# - pydantic (data validation)
```

### 2. Configure Environment
```bash
# Add to .env
SQUAD_SECRET_KEY=your_squad_secret_key
SQUAD_PUBLIC_KEY=your_squad_public_key
SQUAD_WEBHOOK_SECRET=your_webhook_secret
SQUAD_CALLBACK_URL=https://www.urisocial.com/checkout/callback
```

### 3. Create Database Indexes
```bash
python app/scripts/create_billing_indexes.py
```

### 4. Start Server
```bash
# Subscription tiers will auto-seed on startup
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 5. Verify Setup
```bash
# Check subscription tiers initialized
curl https://api.urisocial.com/social-media/billing/subscription/tiers

# Check health
curl https://api.urisocial.com/health
```

---

## 🧪 Testing Checklist (PRD Section 12)

Engineers must validate:

- [ ] Credit deduction works correctly
- [ ] First retry is FREE
- [ ] Second retry deducts 1 credit
- [ ] Text edits do NOT deduct credits
- [ ] Image retry rules enforced
- [ ] Credit exhaustion blocks usage (shows "You've run out of credits")
- [ ] Payment assigns correct credits
- [ ] Low credit warning shows when credits ≤ 3
- [ ] SQUAD webhook verification works
- [ ] Transaction logging captures all events

---

## 📖 API Documentation

Full API docs available at:
- **Swagger UI**: `https://api.urisocial.com/social-media/docs`
- **ReDoc**: `https://api.urisocial.com/social-media/redoc`

All billing endpoints are under `/social-media/billing/*`

---

## 🔐 Security Notes

- JWT authentication required for all endpoints except webhook
- SQUAD webhook signature verification (HMAC-SHA512)
- No credentials stored (SQUAD handles payment processing)
- Transaction audit trail for compliance (PRD 11)

---

## 📝 PRD Compliance Summary

| PRD Requirement | Status | Implementation |
|-----------------|--------|----------------|
| 1 credit = 1 campaign | ✅ | CreditService.deduct_credit() |
| First retry FREE | ✅ | approval_workflow_service.py:325 |
| Second retry costs credit | ✅ | approval_workflow_service.py:351 |
| Unlimited text rewrites | ✅ | text_edit_count tracked, no cost |
| Image retry rules | ✅ | image_retry_count tracked |
| Credit exhaustion blocks | ✅ | complete_social_manager.py:207 |
| Low credit warning (≤3) | ✅ | CreditService.get_credit_balance() |
| SQUAD payment integration | ✅ | PaymentService.py |
| Transaction logging | ✅ | credit_transactions collection |
| Campaign tracking | ✅ | retry_count + image_retry_count fields |
| Monthly credit reset | ✅ | SubscriptionService.allocate_credits() |
| 5 subscription tiers | ✅ | Seeded on startup |

---

## 🆘 Support

For issues or questions:
- Check logs: `docker logs uri-agent.api.prod`
- Check MongoDB: Verify collections exist
- Check SQUAD dashboard: Verify webhooks configured
- API errors: Check Sentry (if configured)

**System must be**: Accurate, Predictable, Transparent (PRD Final Note)
