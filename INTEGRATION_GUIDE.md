# SDK Integration Guide

Complete guide for integrating the API Key authentication system and SDK endpoints into your existing URI Social backend.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Environment Configuration](#environment-configuration)
4. [Database Setup](#database-setup)
5. [Code Integration](#code-integration)
6. [Testing](#testing)
7. [Deployment](#deployment)
8. [Cron Jobs Setup](#cron-jobs-setup)
9. [Troubleshooting](#troubleshooting)

---

## Overview

The SDK system adds:
- **API Key Authentication** for external clients
- **SDK-friendly REST API** at `/api/v1/*`
- **API Key Management Dashboard** at `/social-media/api-keys/*`
- **Rate Limiting** with automatic resets
- **CORS Configuration** for browser requests

**Architecture:**
```
External Client (SDK)
    ↓ (X-API-Key header)
/api/v1/* endpoints
    ↓ (API key middleware)
Existing backend services
    ↓
MongoDB (content, drafts, etc.)
```

---

## Prerequisites

- [x] Existing URI Social backend running
- [x] MongoDB database accessible
- [x] Python 3.8+ with FastAPI
- [x] All SDK files in place:
  - `app/models/api_key.py`
  - `app/middleware/api_key_auth.py`
  - `app/agents/social_media_manager/routers/sdk_router.py`
  - `app/agents/social_media_manager/routers/api_key_management_router.py`
  - `app/config/cors_config.py`
  - `app/cron/reset_api_key_limits.py`

---

## Environment Configuration

### 1. Add Environment Variables

Add these to your `.env` file:

```bash
# === API Key System Configuration ===

# Cron secret for rate limit reset jobs
CRON_SECRET=your-secure-random-secret-here-generate-with-openssl

# Environment (affects CORS)
ENVIRONMENT=production  # or development, staging

# CORS allowed origins (comma-separated)
# Production
CORS_ALLOWED_ORIGINS=https://yourdomain.com,https://app.yourdomain.com

# Development (optional - adds localhost)
CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:3001

# MongoDB (should already exist)
MONGODB_URL=mongodb://localhost:27017
DATABASE_NAME=uri_social

# JWT Secret (should already exist - for API key management dashboard)
JWT_SECRET=your-jwt-secret
```

### 2. Generate Secure Secrets

```bash
# Generate CRON_SECRET
openssl rand -hex 32

# Example output: a7f3d8e2b9c4f1a6e8d5c2b7f4a1e9d3c6b8a5f2e7d4c1b9a6e3f8d5c2b7a4e1
```

### 3. Update `app/core/config.py`

Add these fields to your Settings class:

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # ... existing fields ...

    # API Key System
    CRON_SECRET: str = "your-cron-secret-here"
    ENVIRONMENT: str = "development"
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000"

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
```

---

## Database Setup

### 1. Run Setup Script

This creates all necessary indexes:

```bash
cd /path/to/uri-social-backend

# Run setup script
python -m app.scripts.setup_api_key_system
```

**What this does:**
- Creates indexes on `api_keys` collection:
  - `key_hash` (unique) - for fast API key lookups
  - `user_id` - for listing user's keys
  - `user_id + status` - for active key queries
  - `status` - for admin queries
  - `expires_at` - for cleanup jobs
  - `created_at` - for sorting
- Validates environment configuration
- Tests database connection
- Optionally creates sample API key for testing

### 2. Manual Index Creation (Alternative)

If you prefer manual setup:

```javascript
// Connect to MongoDB
use uri_social

// Create indexes
db.api_keys.createIndex({ "key_hash": 1 }, { unique: true, name: "idx_key_hash_unique" })
db.api_keys.createIndex({ "user_id": 1 }, { name: "idx_user_id" })
db.api_keys.createIndex({ "user_id": 1, "status": 1 }, { name: "idx_user_id_status" })
db.api_keys.createIndex({ "status": 1 }, { name: "idx_status" })
db.api_keys.createIndex({ "expires_at": 1 }, { sparse: true, name: "idx_expires_at" })
db.api_keys.createIndex({ "created_at": 1 }, { name: "idx_created_at" })

// Verify indexes
db.api_keys.getIndexes()
```

---

## Code Integration

### 1. Update `app/main.py`

Add SDK routers to your FastAPI application:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# === IMPORT SDK COMPONENTS ===
from app.agents.social_media_manager.routers.sdk_router import router as sdk_router
from app.agents.social_media_manager.routers.api_key_management_router import router as api_key_mgmt_router
from app.cron.reset_api_key_limits import cron_router
from app.config.cors_config import configure_cors

# ... your existing imports ...

app = FastAPI(
    title="URI Social API",
    version="2.0.0",
    # ... your existing config ...
)

# === CONFIGURE CORS (IMPORTANT: Do this BEFORE adding routers) ===
configure_cors(app)

# === INCLUDE SDK ROUTERS ===

# 1. SDK endpoints for external clients (/api/v1/*)
app.include_router(sdk_router, tags=["SDK"])

# 2. API key management dashboard (/social-media/api-keys/*)
app.include_router(api_key_mgmt_router, tags=["API Keys"])

# 3. Cron jobs for rate limit resets (/cron/*)
app.include_router(cron_router, tags=["Cron Jobs"])

# ... your existing routers ...
app.include_router(social_media_router)
# ... etc ...

# === EXISTING CODE ===
# ... your middleware, startup events, etc ...
```

### 2. Verify Router Registration

Start your server and check the docs:

```bash
# Start server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Open browser to:
# http://localhost:8000/docs
```

**You should see:**
- **SDK** tag with 27 endpoints (`/api/v1/*`)
- **API Keys** tag with 6 endpoints (`/social-media/api-keys/*`)
- **Cron Jobs** tag with 2 endpoints (`/cron/*`)

---

## Testing

### 1. Create Test API Key

**Option A: Via Setup Script**
```bash
python -m app.scripts.setup_api_key_system
# Answer 'y' when asked to create sample API key
```

**Option B: Via Dashboard API**
```bash
# Get JWT token from your login endpoint
JWT_TOKEN="your-jwt-token-here"

# Create API key
curl -X POST http://localhost:8000/social-media/api-keys/create \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Key",
    "description": "For testing SDK integration",
    "environment": "development"
  }'

# Response:
# {
#   "api_key": "uri_sk_abc123...",  <-- SAVE THIS!
#   "key_prefix": "uri_sk_abc123...",
#   "name": "Test Key",
#   "warning": "Store this API key securely. You won't be able to see it again!"
# }
```

### 2. Test SDK Endpoints

```bash
API_KEY="uri_sk_abc123..."  # Replace with your actual key

# Test billing credits endpoint
curl -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/billing/credits

# Expected response:
# {
#   "credits": 100,
#   "subscription": { ... }
# }

# Test content generation
curl -X POST http://localhost:8000/api/v1/content/generate \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "seedContent": "Exciting product launch!",
    "platforms": ["instagram", "facebook"],
    "tone": "professional"
  }'

# Test rate limiting (make 1000+ requests)
for i in {1..1005}; do
  curl -H "X-API-Key: $API_KEY" \
    http://localhost:8000/api/v1/billing/credits
done

# After 1000 requests, you should get:
# HTTP 429 Too Many Requests
# {
#   "detail": "Rate limit exceeded. Try again later."
# }
```

### 3. Test API Key Management

```bash
JWT_TOKEN="your-jwt-token-here"

# List all API keys
curl -H "Authorization: Bearer $JWT_TOKEN" \
  http://localhost:8000/social-media/api-keys/list

# Get specific key details
curl -H "Authorization: Bearer $JWT_TOKEN" \
  http://localhost:8000/social-media/api-keys/{api_key_id}

# Update API key (e.g., change name)
curl -X PATCH http://localhost:8000/social-media/api-keys/{api_key_id} \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Test Key"
  }'

# Revoke API key
curl -X POST http://localhost:8000/social-media/api-keys/{api_key_id}/revoke \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "reason": "No longer needed"
  }'
```

### 4. Test CORS

```bash
# Test preflight request
curl -X OPTIONS http://localhost:8000/api/v1/billing/credits \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: X-API-Key" \
  -v

# Check for these headers in response:
# Access-Control-Allow-Origin: http://localhost:3000
# Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS
# Access-Control-Allow-Headers: Content-Type, Authorization, X-API-Key
```

---

## Deployment

### Production Checklist

- [ ] **Environment Variables Set**
  - [ ] `CRON_SECRET` - Strong random secret
  - [ ] `ENVIRONMENT=production`
  - [ ] `CORS_ALLOWED_ORIGINS` - Your production domains only
  - [ ] `MONGODB_URL` - Production database
  - [ ] `JWT_SECRET` - Existing production secret

- [ ] **Database**
  - [ ] Indexes created on `api_keys` collection
  - [ ] Connection tested from production server

- [ ] **Code Deployed**
  - [ ] All SDK files deployed
  - [ ] `main.py` updated with router includes
  - [ ] CORS configured BEFORE routers

- [ ] **Cron Jobs Configured** (see next section)
  - [ ] Hourly rate limit reset
  - [ ] Daily rate limit reset

- [ ] **Security**
  - [ ] CORS origins limited to production domains
  - [ ] `CRON_SECRET` is strong and secret
  - [ ] Rate limits configured per tier
  - [ ] HTTPS enforced (handled by your reverse proxy)

- [ ] **Monitoring**
  - [ ] Log aggregation for API key usage
  - [ ] Alerts for rate limit exceeded
  - [ ] Dashboard for API key metrics

### Deployment Commands

```bash
# 1. Pull latest code
git pull origin main

# 2. Install dependencies (if any new ones)
pip install -r requirements.txt

# 3. Run database setup
python -m app.scripts.setup_api_key_system

# 4. Restart application
systemctl restart uri-social-backend
# or
pm2 restart uri-social-backend
# or
supervisorctl restart uri-social-backend

# 5. Verify endpoints
curl -I https://api.yourdomain.com/api/v1/billing/credits

# 6. Check logs
tail -f /var/log/uri-social/app.log
```

---

## Cron Jobs Setup

Rate limits need to be reset hourly and daily via cron jobs.

### Option 1: HTTP Endpoints (Recommended)

Set up cron jobs that call your API:

```bash
# Edit crontab
crontab -e

# Add these lines:

# Reset hourly rate limits every hour
0 * * * * curl -X POST https://api.yourdomain.com/cron/reset-hourly-limits \
  -H "X-Cron-Secret: YOUR_CRON_SECRET_HERE" \
  >> /var/log/uri-social-cron-hourly.log 2>&1

# Reset daily rate limits at midnight UTC
0 0 * * * curl -X POST https://api.yourdomain.com/cron/reset-daily-limits \
  -H "X-Cron-Secret: YOUR_CRON_SECRET_HERE" \
  >> /var/log/uri-social-cron-daily.log 2>&1
```

**Replace:**
- `https://api.yourdomain.com` with your actual API URL
- `YOUR_CRON_SECRET_HERE` with your actual `CRON_SECRET` from `.env`

### Option 2: Direct Python Script

Alternatively, run the Python script directly:

```bash
# Edit crontab
crontab -e

# Add these lines:

# Reset hourly limits every hour
0 * * * * cd /path/to/uri-social-backend && \
  /path/to/venv/bin/python app/cron/reset_api_key_limits.py hourly \
  >> /var/log/uri-social-cron-hourly.log 2>&1

# Reset daily limits at midnight UTC
0 0 * * * cd /path/to/uri-social-backend && \
  /path/to/venv/bin/python app/cron/reset_api_key_limits.py daily \
  >> /var/log/uri-social-cron-daily.log 2>&1
```

**Replace:**
- `/path/to/uri-social-backend` with actual path
- `/path/to/venv/bin/python` with your Python interpreter path

### Testing Cron Jobs

```bash
# Test manually
CRON_SECRET="your-secret-here"

# Test hourly reset
curl -X POST http://localhost:8000/cron/reset-hourly-limits \
  -H "X-Cron-Secret: $CRON_SECRET" \
  -v

# Expected response:
# {
#   "success": true,
#   "message": "Hourly rate limits reset successfully",
#   "timestamp": "2024-01-15T12:00:00"
# }

# Check logs
tail -f /var/log/uri-social-cron-hourly.log
```

### Monitoring Cron Jobs

Create a simple monitoring script:

```bash
#!/bin/bash
# /usr/local/bin/check-uri-social-cron.sh

HOURLY_LOG="/var/log/uri-social-cron-hourly.log"
DAILY_LOG="/var/log/uri-social-cron-daily.log"

# Check if hourly reset ran in the last 90 minutes
if [ -f "$HOURLY_LOG" ]; then
  LAST_RUN=$(stat -f %m "$HOURLY_LOG")
  NOW=$(date +%s)
  DIFF=$((NOW - LAST_RUN))

  if [ $DIFF -gt 5400 ]; then  # 90 minutes
    echo "⚠️  WARNING: Hourly cron job has not run in $((DIFF / 60)) minutes"
  else
    echo "✅ Hourly cron job is running"
  fi
fi

# Check for errors in logs
if grep -q "Failed to reset" "$HOURLY_LOG" 2>/dev/null; then
  echo "❌ ERROR: Hourly cron job has errors"
fi
```

---

## Troubleshooting

### Issue: "Invalid API key format"

**Symptom:** 401 error with message "Invalid API key format"

**Causes:**
1. API key doesn't start with `uri_sk_`
2. Missing `X-API-Key` header
3. Typo in API key

**Solution:**
```bash
# Verify API key format
echo $API_KEY | grep "^uri_sk_"

# Check request headers
curl -v -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/billing/credits
# Should see: X-API-Key: uri_sk_...
```

### Issue: "Invalid or revoked API key"

**Symptom:** 401 error with message "Invalid or revoked API key"

**Causes:**
1. API key doesn't exist in database
2. API key status is "revoked" or "expired"
3. Wrong MongoDB database

**Solution:**
```bash
# Check API key in database
mongosh

use uri_social
db.api_keys.findOne({ status: "active" })

# Check if key_hash matches
python3 << EOF
import hashlib
api_key = "uri_sk_your_key_here"
key_hash = hashlib.sha256(api_key.encode()).hexdigest()
print(f"Key hash: {key_hash}")
EOF

# Then search in MongoDB:
db.api_keys.findOne({ key_hash: "hash_from_above" })
```

### Issue: "Rate limit exceeded"

**Symptom:** 429 error with message "Rate limit exceeded"

**Causes:**
1. API key has made too many requests this hour
2. Cron jobs not running to reset limits

**Solution:**
```bash
# Check current usage
mongosh

use uri_social
db.api_keys.find({ status: "active" }, {
  name: 1,
  "usage_stats.requests_this_hour": 1,
  "rate_limits.requests_per_hour": 1
})

# Manually reset hourly limits (for testing)
curl -X POST http://localhost:8000/cron/reset-hourly-limits \
  -H "X-Cron-Secret: $CRON_SECRET"

# Check cron logs
tail -f /var/log/uri-social-cron-hourly.log
```

### Issue: CORS errors in browser

**Symptom:** Browser console shows CORS error

**Causes:**
1. Origin not in `CORS_ALLOWED_ORIGINS`
2. CORS middleware not configured
3. CORS middleware added AFTER routers

**Solution:**
```bash
# 1. Check environment variable
echo $CORS_ALLOWED_ORIGINS

# 2. Verify origin is included
# In .env:
CORS_ALLOWED_ORIGINS=http://localhost:3000,https://yourdomain.com

# 3. Ensure CORS is configured BEFORE routers in main.py
# CORRECT:
configure_cors(app)
app.include_router(sdk_router)

# WRONG:
app.include_router(sdk_router)
configure_cors(app)  # Too late!

# 4. Test CORS
curl -X OPTIONS http://localhost:8000/api/v1/billing/credits \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: GET" \
  -v
```

### Issue: Cron jobs not resetting limits

**Symptom:** Rate limits never reset

**Causes:**
1. Cron jobs not configured
2. Wrong `CRON_SECRET`
3. Cron job can't reach API

**Solution:**
```bash
# 1. Check if cron jobs are configured
crontab -l | grep uri-social

# 2. Test cron endpoint manually
curl -X POST http://localhost:8000/cron/reset-hourly-limits \
  -H "X-Cron-Secret: $CRON_SECRET" \
  -v

# 3. Check cron logs
tail -f /var/log/uri-social-cron-hourly.log

# 4. Verify CRON_SECRET matches
grep CRON_SECRET .env
# Should match header in cron job

# 5. Test direct Python script
cd /path/to/uri-social-backend
python app/cron/reset_api_key_limits.py hourly
```

### Issue: "Insufficient permissions"

**Symptom:** 403 error with message "Insufficient permissions"

**Causes:**
1. API key doesn't have required scope
2. Endpoint requires different scope than key has

**Solution:**
```bash
# Check API key scopes
mongosh

use uri_social
db.api_keys.findOne(
  { status: "active" },
  { name: 1, scopes: 1 }
)

# Common scopes needed:
# - content:write (for /api/v1/content/generate)
# - drafts:read (for /api/v1/drafts/*)
# - images:generate (for /api/v1/images/generate)

# Update API key scopes via dashboard or directly:
db.api_keys.updateOne(
  { _id: ObjectId("...") },
  { $addToSet: { scopes: "content:write" } }
)
```

---

## Support

For issues or questions:

1. Check this guide
2. Review `/docs` endpoint for API reference
3. Check logs: `/var/log/uri-social/app.log`
4. Contact: support@urisocial.com

---

## Summary

You've successfully integrated the SDK system! 🎉

**What you have now:**
- ✅ API key authentication system
- ✅ SDK endpoints at `/api/v1/*`
- ✅ API key management dashboard
- ✅ Rate limiting with automatic resets
- ✅ CORS configuration for browser requests
- ✅ Comprehensive testing tools

**Next steps:**
1. Create API keys for your users
2. Distribute SDK packages (npm/PyPI)
3. Share documentation with clients
4. Monitor usage and rate limits

**Resources:**
- SDK Documentation: `/urisocial-sdk/START_HERE.md`
- Client Setup Guide: `/urisocial-sdk/SETUP_FOR_CLIENTS.md`
- API Reference: `http://localhost:8000/docs`
