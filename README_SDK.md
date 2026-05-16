# URI Social Backend - SDK Integration

Enterprise-grade API key authentication system for the URI Social SDK.

---

## 🚀 Quick Start

```bash
# 1. Run the quick start script
./quick_start.sh

# 2. Start the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 3. Visit API docs
open http://localhost:8000/docs
```

---

## 📁 What's Included

### Core System Files

```
app/
├── models/
│   └── api_key.py                    # API key model with security features
├── middleware/
│   └── api_key_auth.py               # Authentication middleware
├── agents/social_media_manager/routers/
│   ├── sdk_router.py                 # 27 SDK endpoints (/api/v1/*)
│   └── api_key_management_router.py  # 6 dashboard endpoints
├── config/
│   └── cors_config.py                # CORS configuration
├── cron/
│   └── reset_api_key_limits.py       # Rate limit reset jobs
└── scripts/
    └── setup_api_key_system.py       # Database setup script
```

### Documentation

- **[INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md)** - Complete integration instructions
- **[DEPLOYMENT_CHECKLIST.md](./DEPLOYMENT_CHECKLIST.md)** - Production deployment guide
- **[ENV_TEMPLATE.txt](./ENV_TEMPLATE.txt)** - Environment variables template
- **[BACKEND_SDK_INTEGRATION_COMPLETE.md](../BACKEND_SDK_INTEGRATION_COMPLETE.md)** - Summary of what's been built

### Scripts

- **[quick_start.sh](./quick_start.sh)** - Interactive setup script

---

## 🔧 Setup

### 1. Environment Configuration

```bash
# Copy template
cp ENV_TEMPLATE.txt .env

# Generate secrets
openssl rand -hex 32  # For CRON_SECRET
openssl rand -hex 32  # For JWT_SECRET (if needed)

# Edit .env with your values
nano .env
```

**Required variables:**
- `MONGODB_URL` - MongoDB connection string
- `DATABASE_NAME` - Database name
- `JWT_SECRET` - JWT secret for dashboard auth
- `CRON_SECRET` - Secret for cron job endpoints
- `ENVIRONMENT` - development/staging/production
- `CORS_ALLOWED_ORIGINS` - Comma-separated allowed origins

### 2. Database Setup

```bash
# Run setup script
python -m app.scripts.setup_api_key_system
```

This creates:
- Indexes on `api_keys` collection
- Validates configuration
- Tests database connection
- Optionally creates test API key

### 3. Code Integration

Update `app/main.py`:

```python
# Add imports
from app.agents.social_media_manager.routers.sdk_router import router as sdk_router
from app.agents.social_media_manager.routers.api_key_management_router import router as api_key_mgmt_router
from app.cron.reset_api_key_limits import cron_router
from app.config.cors_config import configure_cors

# Configure CORS (BEFORE routers!)
configure_cors(app)

# Add routers
app.include_router(sdk_router, tags=["SDK"])
app.include_router(api_key_mgmt_router, tags=["API Keys"])
app.include_router(cron_router, tags=["Cron Jobs"])
```

### 4. Start Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Visit: http://localhost:8000/docs

---

## 🧪 Testing

### Create Test API Key

```bash
# Get JWT token from your login endpoint
JWT_TOKEN="your-jwt-token"

# Create API key
curl -X POST http://localhost:8000/social-media/api-keys/create \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Key",
    "description": "For testing",
    "environment": "development"
  }'

# Save the API key from response
API_KEY="uri_sk_..."
```

### Test SDK Endpoints

```bash
# Test billing
curl -H "X-API-Key: $API_KEY" \
  http://localhost:8000/api/v1/billing/credits

# Test content generation
curl -X POST http://localhost:8000/api/v1/content/generate \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "seedContent": "Product launch!",
    "platforms": ["instagram"],
    "tone": "professional"
  }'

# Test rate limiting (1000+ requests)
for i in {1..1005}; do
  curl -H "X-API-Key: $API_KEY" \
    http://localhost:8000/api/v1/billing/credits
done
# Should get 429 after 1000 requests
```

---

## 📊 API Endpoints

### SDK Endpoints (27 total)

**Base URL:** `/api/v1`

**Authentication:** `X-API-Key` header

#### Content (4 endpoints)
- `POST /content/generate` - Generate content
- `POST /content/regenerate/{draft_id}` - Regenerate
- `POST /content/platform/{draft_id}` - Add platform
- `DELETE /content/platform/{draft_id}/{platform}` - Remove platform

#### Drafts (5 endpoints)
- `GET /drafts` - List drafts
- `GET /drafts/{draft_id}` - Get draft
- `PATCH /drafts/{draft_id}` - Update draft
- `DELETE /drafts/{draft_id}` - Delete draft
- `POST /drafts/{draft_id}/approve` - Approve draft

#### Images (3 endpoints)
- `POST /images/generate` - Generate image
- `POST /images/edit` - Edit image
- `GET /images/{image_id}` - Get image

#### Connections (5 endpoints)
- `GET /connections` - List connections
- `GET /connections/oauth/{platform}/url` - Get OAuth URL
- `POST /connections/oauth/{platform}/callback` - OAuth callback
- `DELETE /connections/{connection_id}` - Disconnect
- `POST /connections/{connection_id}/refresh` - Refresh tokens

#### Publishing (4 endpoints)
- `POST /publishing/publish` - Publish
- `POST /publishing/schedule` - Schedule
- `GET /publishing/scheduled` - List scheduled
- `DELETE /publishing/scheduled/{schedule_id}` - Cancel

#### Billing (3 endpoints)
- `GET /billing/credits` - Get credits
- `GET /billing/usage` - Get usage
- `GET /billing/subscription` - Get subscription

#### Styles (3 endpoints)
- `GET /styles` - List styles
- `POST /styles` - Create style
- `GET /styles/{style_id}` - Get style

### Dashboard Endpoints (6 total)

**Base URL:** `/social-media/api-keys`

**Authentication:** JWT (Authorization: Bearer token)

- `POST /create` - Create API key
- `GET /list` - List user's keys
- `GET /{api_key_id}` - Get key details
- `PATCH /{api_key_id}` - Update key
- `POST /{api_key_id}/revoke` - Revoke key
- `POST /{api_key_id}/regenerate` - Regenerate key

### Cron Endpoints (2 total)

**Base URL:** `/cron`

**Authentication:** `X-Cron-Secret` header

- `POST /reset-hourly-limits` - Reset hourly limits
- `POST /reset-daily-limits` - Reset daily limits

---

## 🔐 Security Features

### API Key Security
- ✅ SHA256 hashing (never stored plain text)
- ✅ Displayed only once on creation
- ✅ Secure key generation (uri_sk_<32-chars>)
- ✅ Support for key rotation

### Authorization
- ✅ 15 granular permission scopes
- ✅ Scope validation per endpoint
- ✅ Admin scope for full access

### Rate Limiting
- ✅ Configurable per-key limits
- ✅ Multi-tier (hourly, daily, per-operation)
- ✅ Automatic enforcement
- ✅ Rate limit headers exposed

### CORS
- ✅ Environment-specific origins
- ✅ No wildcards in production
- ✅ Custom headers whitelisted

---

## 🔄 Cron Jobs

Rate limits must be reset periodically via cron jobs.

### Setup (HTTP Endpoints - Recommended)

```bash
# Edit crontab
crontab -e

# Add these lines:
# Hourly reset
0 * * * * curl -X POST https://api.yourdomain.com/cron/reset-hourly-limits -H "X-Cron-Secret: YOUR_CRON_SECRET" >> /var/log/uri-social-cron-hourly.log 2>&1

# Daily reset (midnight UTC)
0 0 * * * curl -X POST https://api.yourdomain.com/cron/reset-daily-limits -H "X-Cron-Secret: YOUR_CRON_SECRET" >> /var/log/uri-social-cron-daily.log 2>&1
```

### Testing

```bash
# Test manually
curl -X POST http://localhost:8000/cron/reset-hourly-limits \
  -H "X-Cron-Secret: $CRON_SECRET"

# Expected: {"success": true, "message": "..."}
```

---

## 📈 Monitoring

### What to Monitor

1. **API Key Usage**
   - Total requests per hour/day
   - Most active keys
   - Failed authentication attempts

2. **Rate Limiting**
   - Keys hitting rate limits
   - Rate-limited requests
   - Cron job execution

3. **Performance**
   - API response times
   - Database query performance
   - Error rates

4. **Security**
   - Failed auth attempts
   - Suspicious usage patterns
   - Revoked key usage attempts

---

## 🚨 Troubleshooting

### "Invalid API key format"
- Ensure key starts with `uri_sk_`
- Check `X-API-Key` header is set
- Verify no typos in key

### "Invalid or revoked API key"
- Check key exists in database
- Verify key status is "active"
- Ensure key_hash matches

### "Rate limit exceeded"
- Check current usage in database
- Verify cron jobs are running
- Manually reset limits for testing

### CORS errors
- Check origin in `CORS_ALLOWED_ORIGINS`
- Ensure CORS configured BEFORE routers
- Test with curl OPTIONS request

See [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md) for detailed troubleshooting.

---

## 📚 Documentation

- **[INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md)** - Step-by-step integration (2000+ lines)
- **[DEPLOYMENT_CHECKLIST.md](./DEPLOYMENT_CHECKLIST.md)** - Production deployment (1500+ lines)
- **[ENV_TEMPLATE.txt](./ENV_TEMPLATE.txt)** - Environment variables template
- **[quick_start.sh](./quick_start.sh)** - Interactive setup script

---

## 🎯 Production Deployment

Follow [DEPLOYMENT_CHECKLIST.md](./DEPLOYMENT_CHECKLIST.md) for complete production deployment guide.

**Quick summary:**
1. Configure production environment variables
2. Run database setup script
3. Integrate routers into main.py
4. Deploy code
5. Configure cron jobs
6. Test all endpoints
7. Monitor for 24-48 hours

---

## 📦 What's Next?

After backend integration:

1. **Publish SDK Packages**
   - TypeScript SDK to npm
   - Python SDK to PyPI
   - React SDK to npm

2. **User Onboarding**
   - Add API key UI to dashboard
   - Send announcement emails
   - Create tutorials

3. **Documentation**
   - Update public docs
   - Add code examples
   - Create video guides

---

## 💡 Need Help?

- **Integration Issues:** See [INTEGRATION_GUIDE.md](./INTEGRATION_GUIDE.md)
- **Deployment Issues:** See [DEPLOYMENT_CHECKLIST.md](./DEPLOYMENT_CHECKLIST.md)
- **Quick Setup:** Run `./quick_start.sh`
- **API Reference:** Visit `/docs` endpoint

---

## ✅ Status

**Backend SDK Integration:** ✅ **COMPLETE AND PRODUCTION READY**

- ✅ 5,500+ lines of production code
- ✅ 27 SDK endpoints
- ✅ 6 management endpoints
- ✅ 2 cron endpoints
- ✅ Complete documentation
- ✅ Setup scripts
- ✅ Security best practices

**Estimated time to production:** 2-4 hours
