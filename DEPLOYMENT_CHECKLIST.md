# SDK System Deployment Checklist

Complete checklist for deploying the API key authentication system and SDK endpoints to production.

---

## Pre-Deployment

### 1. Code Review
- [ ] All SDK files are committed to version control
- [ ] No hardcoded secrets or API keys in code
- [ ] All imports are correct and tested
- [ ] Error handling is comprehensive
- [ ] Logging is configured properly

### 2. Environment Configuration
- [ ] `.env` file configured with production values
- [ ] `ENVIRONMENT=production` is set
- [ ] `CRON_SECRET` is strong random secret (32+ chars)
- [ ] `JWT_SECRET` is secure (if not already set)
- [ ] `CORS_ALLOWED_ORIGINS` lists only production domains
- [ ] `MONGODB_URL` points to production database

**Generate secrets:**
```bash
# Generate CRON_SECRET
openssl rand -hex 32

# Generate JWT_SECRET (if needed)
openssl rand -hex 32
```

### 3. Database Preparation
- [ ] MongoDB connection tested from production server
- [ ] Database backup created
- [ ] Connection string has proper authentication
- [ ] Database user has correct permissions

**Test connection:**
```bash
mongosh "$MONGODB_URL"
```

---

## Deployment Steps

### Step 1: Deploy Code

- [ ] Pull latest code to production server
```bash
cd /path/to/uri-social-backend
git pull origin main
```

- [ ] Install/update dependencies
```bash
pip install -r requirements.txt
```

- [ ] Verify all SDK files are present
```bash
ls -la app/models/api_key.py
ls -la app/middleware/api_key_auth.py
ls -la app/agents/social_media_manager/routers/sdk_router.py
ls -la app/agents/social_media_manager/routers/api_key_management_router.py
ls -la app/config/cors_config.py
ls -la app/cron/reset_api_key_limits.py
ls -la app/scripts/setup_api_key_system.py
```

### Step 2: Database Setup

- [ ] Run database setup script
```bash
python -m app.scripts.setup_api_key_system
```

**What this does:**
- Creates indexes on `api_keys` collection
- Validates environment configuration
- Tests database connection

**Expected output:**
```
🚀 URI Social API Key System Setup
🔍 Validating configuration...
✅ MONGODB_URL: mongodb://...
✅ DATABASE_NAME: uri_social
✅ CRON_SECRET: ********************
✅ ENVIRONMENT: production
✅ JWT_SECRET: ********************
🧪 Testing database connection...
✅ Successfully connected to MongoDB
📊 Creating indexes for api_keys collection...
✅ Created unique index on key_hash
✅ Created index on user_id
✅ Created compound index on user_id + status
✅ Created index on status
✅ Created sparse index on expires_at
✅ Created index on created_at
✅ Setup complete!
```

- [ ] Verify indexes were created
```bash
mongosh "$MONGODB_URL"

use uri_social
db.api_keys.getIndexes()
```

**Expected indexes:**
1. `_id_` (default)
2. `idx_key_hash_unique` on `key_hash`
3. `idx_user_id` on `user_id`
4. `idx_user_id_status` on `user_id + status`
5. `idx_status` on `status`
6. `idx_expires_at` on `expires_at`
7. `idx_created_at` on `created_at`

### Step 3: Code Integration

- [ ] Update `app/main.py` to include SDK routers

**Add these imports:**
```python
from app.agents.social_media_manager.routers.sdk_router import router as sdk_router
from app.agents.social_media_manager.routers.api_key_management_router import router as api_key_mgmt_router
from app.cron.reset_api_key_limits import cron_router
from app.config.cors_config import configure_cors
```

**Configure CORS (BEFORE routers):**
```python
app = FastAPI(title="URI Social API", version="2.0.0")

# IMPORTANT: Configure CORS BEFORE adding routers
configure_cors(app)
```

**Add routers:**
```python
# SDK endpoints for external clients
app.include_router(sdk_router, tags=["SDK"])

# API key management dashboard
app.include_router(api_key_mgmt_router, tags=["API Keys"])

# Cron jobs for rate limit resets
app.include_router(cron_router, tags=["Cron Jobs"])

# ... your existing routers ...
```

- [ ] Commit changes
```bash
git add app/main.py
git commit -m "feat: integrate SDK authentication system"
git push origin main
```

### Step 4: Restart Application

- [ ] Restart backend application

**Using systemd:**
```bash
sudo systemctl restart uri-social-backend
sudo systemctl status uri-social-backend
```

**Using PM2:**
```bash
pm2 restart uri-social-backend
pm2 logs uri-social-backend --lines 50
```

**Using Supervisor:**
```bash
sudo supervisorctl restart uri-social-backend
sudo supervisorctl tail -f uri-social-backend
```

**Direct (development):**
```bash
pkill -f "uvicorn app.main:app"
uvicorn app.main:app --host 0.0.0.0 --port 8000 &
```

### Step 5: Verify Deployment

- [ ] Check application logs for startup errors
```bash
# systemd
sudo journalctl -u uri-social-backend -f

# PM2
pm2 logs uri-social-backend

# Supervisor
sudo supervisorctl tail -f uri-social-backend

# Direct
tail -f /var/log/uri-social/app.log
```

- [ ] Verify endpoints are registered
```bash
curl -I https://api.yourdomain.com/docs
```

**Open in browser:** `https://api.yourdomain.com/docs`

**Expected sections:**
- **SDK** tag with ~27 endpoints (`/api/v1/*`)
- **API Keys** tag with ~6 endpoints (`/social-media/api-keys/*`)
- **Cron Jobs** tag with 2 endpoints (`/cron/*`)

- [ ] Test health check
```bash
curl https://api.yourdomain.com/health
```

---

## Testing in Production

### Test 1: Create API Key

- [ ] Login to dashboard and get JWT token
```bash
# Login (use your actual login endpoint)
curl -X POST https://api.yourdomain.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "your@email.com", "password": "password"}'

# Save JWT token
JWT_TOKEN="eyJhbGc..."
```

- [ ] Create test API key
```bash
curl -X POST https://api.yourdomain.com/social-media/api-keys/create \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production Test Key",
    "description": "For testing production deployment",
    "environment": "production"
  }'
```

**Expected response:**
```json
{
  "api_key": "uri_sk_abc123def456...",
  "api_key_id": "507f1f77bcf86cd799439011",
  "key_prefix": "uri_sk_abc123...",
  "name": "Production Test Key",
  "scopes": ["content:read", "content:write", ...],
  "environment": "production",
  "created_at": "2024-01-15T12:00:00",
  "warning": "Store this API key securely. You won't be able to see it again!"
}
```

- [ ] **SAVE THE API KEY!** You won't see it again.
```bash
API_KEY="uri_sk_abc123def456..."  # From response above
```

### Test 2: SDK Endpoints

- [ ] Test billing credits endpoint
```bash
curl -H "X-API-Key: $API_KEY" \
  https://api.yourdomain.com/api/v1/billing/credits
```

**Expected:** 200 OK with credits data

- [ ] Test content generation
```bash
curl -X POST https://api.yourdomain.com/api/v1/content/generate \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "seedContent": "Exciting product launch!",
    "platforms": ["instagram"],
    "tone": "professional"
  }'
```

**Expected:** 201 Created with generated content

- [ ] Test drafts list
```bash
curl -H "X-API-Key: $API_KEY" \
  https://api.yourdomain.com/api/v1/drafts
```

**Expected:** 200 OK with list of drafts

### Test 3: CORS

- [ ] Test CORS preflight from allowed origin
```bash
curl -X OPTIONS https://api.yourdomain.com/api/v1/billing/credits \
  -H "Origin: https://yourdomain.com" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: X-API-Key" \
  -v
```

**Expected headers:**
```
Access-Control-Allow-Origin: https://yourdomain.com
Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization, X-API-Key
```

- [ ] Test CORS from disallowed origin (should fail)
```bash
curl -X OPTIONS https://api.yourdomain.com/api/v1/billing/credits \
  -H "Origin: https://evil-site.com" \
  -H "Access-Control-Request-Method: GET" \
  -v
```

**Expected:** No `Access-Control-Allow-Origin` header

### Test 4: Rate Limiting

- [ ] Make 1000+ requests to trigger rate limit
```bash
# Note: This will consume rate limit for test API key
for i in {1..1005}; do
  echo "Request $i"
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "X-API-Key: $API_KEY" \
    https://api.yourdomain.com/api/v1/billing/credits
done
```

**Expected:**
- First 1000 requests: 200 OK
- Remaining requests: 429 Too Many Requests

- [ ] Verify rate limit headers
```bash
curl -v -H "X-API-Key: $API_KEY" \
  https://api.yourdomain.com/api/v1/billing/credits
```

**Expected headers:**
```
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
```

### Test 5: Authentication Errors

- [ ] Test with invalid API key
```bash
curl -H "X-API-Key: uri_sk_invalid" \
  https://api.yourdomain.com/api/v1/billing/credits
```

**Expected:** 401 Unauthorized

- [ ] Test with missing API key
```bash
curl https://api.yourdomain.com/api/v1/billing/credits
```

**Expected:** 401 Unauthorized

- [ ] Test with revoked API key (revoke first via dashboard)
```bash
# Revoke the key
curl -X POST https://api.yourdomain.com/social-media/api-keys/$API_KEY_ID/revoke \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Testing revocation"}'

# Try to use revoked key
curl -H "X-API-Key: $API_KEY" \
  https://api.yourdomain.com/api/v1/billing/credits
```

**Expected:** 401 Unauthorized

---

## Cron Jobs Setup

### Step 1: Configure Cron Jobs

- [ ] Choose cron method (HTTP endpoints recommended)

**Method A: HTTP Endpoints (Recommended)**

```bash
# Edit crontab
crontab -e

# Add these lines:
# Reset hourly rate limits every hour
0 * * * * curl -X POST https://api.yourdomain.com/cron/reset-hourly-limits -H "X-Cron-Secret: YOUR_CRON_SECRET" >> /var/log/uri-social-cron-hourly.log 2>&1

# Reset daily rate limits at midnight UTC
0 0 * * * curl -X POST https://api.yourdomain.com/cron/reset-daily-limits -H "X-Cron-Secret: YOUR_CRON_SECRET" >> /var/log/uri-social-cron-daily.log 2>&1
```

**Method B: Direct Python Script**

```bash
# Edit crontab
crontab -e

# Add these lines:
# Reset hourly limits
0 * * * * cd /path/to/uri-social-backend && /path/to/venv/bin/python app/cron/reset_api_key_limits.py hourly >> /var/log/uri-social-cron-hourly.log 2>&1

# Reset daily limits
0 0 * * * cd /path/to/uri-social-backend && /path/to/venv/bin/python app/cron/reset_api_key_limits.py daily >> /var/log/uri-social-cron-daily.log 2>&1
```

- [ ] Create log files
```bash
sudo touch /var/log/uri-social-cron-hourly.log
sudo touch /var/log/uri-social-cron-daily.log
sudo chmod 666 /var/log/uri-social-cron-*.log
```

### Step 2: Test Cron Jobs

- [ ] Test hourly reset manually
```bash
curl -X POST https://api.yourdomain.com/cron/reset-hourly-limits \
  -H "X-Cron-Secret: $CRON_SECRET" \
  -v
```

**Expected response:**
```json
{
  "success": true,
  "message": "Hourly rate limits reset successfully",
  "timestamp": "2024-01-15T12:00:00"
}
```

- [ ] Test daily reset manually
```bash
curl -X POST https://api.yourdomain.com/cron/reset-daily-limits \
  -H "X-Cron-Secret: $CRON_SECRET" \
  -v
```

- [ ] Verify rate limits were reset in database
```bash
mongosh "$MONGODB_URL"

use uri_social
db.api_keys.find({}, {
  name: 1,
  "usage_stats.requests_this_hour": 1,
  "usage_stats.requests_today": 1
})
```

**Expected:** All counters should be 0

- [ ] Wait for cron to run (or trigger manually)
```bash
# After cron runs, check logs
tail -f /var/log/uri-social-cron-hourly.log
```

### Step 3: Monitor Cron Jobs

- [ ] Set up cron monitoring script
```bash
sudo nano /usr/local/bin/check-uri-social-cron.sh
```

**Add:**
```bash
#!/bin/bash
HOURLY_LOG="/var/log/uri-social-cron-hourly.log"
NOW=$(date +%s)

if [ -f "$HOURLY_LOG" ]; then
  LAST_RUN=$(stat -f %m "$HOURLY_LOG" 2>/dev/null || stat -c %Y "$HOURLY_LOG")
  DIFF=$((NOW - LAST_RUN))

  if [ $DIFF -gt 5400 ]; then  # 90 minutes
    echo "⚠️  WARNING: Cron not running (last run: $((DIFF / 60)) min ago)"
    exit 1
  else
    echo "✅ Cron is running (last run: $((DIFF / 60)) min ago)"
    exit 0
  fi
fi
```

- [ ] Make executable
```bash
sudo chmod +x /usr/local/bin/check-uri-social-cron.sh
```

- [ ] Test monitoring script
```bash
/usr/local/bin/check-uri-social-cron.sh
```

- [ ] Add to monitoring system (optional)
```bash
# Add to crontab to alert if cron fails
*/30 * * * * /usr/local/bin/check-uri-social-cron.sh || mail -s "URI Social Cron Failed" admin@yourdomain.com
```

---

## Security Checklist

- [ ] **Secrets Management**
  - [ ] `CRON_SECRET` is not committed to git
  - [ ] `.env` file is in `.gitignore`
  - [ ] Secrets are stored in secure vault (AWS Secrets Manager, etc.)
  - [ ] Environment variables are set correctly on server

- [ ] **CORS Configuration**
  - [ ] Only production domains in `CORS_ALLOWED_ORIGINS`
  - [ ] No wildcards (`*`) in production
  - [ ] Test that unauthorized origins are blocked

- [ ] **Rate Limiting**
  - [ ] Default rate limits are reasonable
  - [ ] Cron jobs are running to reset limits
  - [ ] Monitor for rate limit abuse

- [ ] **API Keys**
  - [ ] Keys are hashed with SHA256
  - [ ] Full keys are never logged
  - [ ] Keys are only shown once on creation
  - [ ] Revoked keys cannot be used

- [ ] **HTTPS**
  - [ ] All production endpoints use HTTPS
  - [ ] HTTP requests redirect to HTTPS
  - [ ] SSL certificates are valid

- [ ] **Logging**
  - [ ] API key usage is logged (without exposing full keys)
  - [ ] Failed authentication attempts are logged
  - [ ] Rate limit violations are logged
  - [ ] Logs are aggregated and monitored

---

## Monitoring & Alerts

### Set up monitoring for:

- [ ] **API Key Usage**
  - Total requests per hour/day
  - Most active API keys
  - Failed authentication attempts

- [ ] **Rate Limiting**
  - Number of rate-limited requests
  - Keys hitting rate limits frequently
  - Cron job success/failure

- [ ] **Errors**
  - 4xx errors (authentication, rate limits)
  - 5xx errors (server issues)
  - Database connection errors

- [ ] **Performance**
  - API response times
  - Database query performance
  - Cache hit rates

### Recommended tools:

- [ ] Application monitoring: Datadog, New Relic, Sentry
- [ ] Log aggregation: ELK Stack, Splunk, Papertrail
- [ ] Uptime monitoring: Pingdom, UptimeRobot
- [ ] Custom dashboards: Grafana + Prometheus

---

## Post-Deployment

### Immediate (Day 1)

- [ ] Verify all endpoints are accessible
- [ ] Create API keys for test accounts
- [ ] Monitor logs for errors
- [ ] Test cron jobs run successfully
- [ ] Document any issues

### Week 1

- [ ] Monitor API key usage patterns
- [ ] Adjust rate limits if needed
- [ ] Review error logs
- [ ] Ensure cron jobs are running
- [ ] Collect feedback from early users

### Week 2-4

- [ ] Analyze usage metrics
- [ ] Optimize rate limits per tier
- [ ] Review security logs
- [ ] Update documentation based on feedback
- [ ] Plan for SDK package publishing

---

## Rollback Plan

If issues arise, rollback procedure:

1. **Disable SDK endpoints**
```python
# In app/main.py, comment out:
# app.include_router(sdk_router, tags=["SDK"])
```

2. **Restart application**
```bash
sudo systemctl restart uri-social-backend
```

3. **Verify existing functionality still works**
```bash
curl https://api.yourdomain.com/health
```

4. **Investigate issues**
```bash
tail -n 1000 /var/log/uri-social/app.log | grep ERROR
```

5. **Fix and redeploy**
- Fix issues in development
- Test thoroughly
- Deploy with this checklist again

---

## Success Criteria

Deployment is successful when:

- [x] All SDK endpoints return 200/201 responses with valid API keys
- [x] Invalid API keys return 401 errors
- [x] Rate limiting triggers at configured thresholds
- [x] Cron jobs run and reset rate limits
- [x] CORS allows configured origins only
- [x] No errors in application logs
- [x] API key management dashboard works
- [x] Performance is acceptable (< 500ms p95)
- [x] All tests pass

---

## Next Steps

After successful deployment:

1. **SDK Package Publishing**
   - [ ] Publish `@urisocial/sdk` to npm
   - [ ] Publish `urisocial` to PyPI
   - [ ] Publish `@urisocial/react` to npm

2. **Documentation**
   - [ ] Update public API documentation
   - [ ] Create SDK integration guides
   - [ ] Add code examples to docs

3. **User Onboarding**
   - [ ] Add API key creation to dashboard
   - [ ] Send onboarding emails with SDK links
   - [ ] Create video tutorials

4. **Marketing**
   - [ ] Announce SDK availability
   - [ ] Write blog post
   - [ ] Share on social media

---

## Contact

For deployment support:
- Technical Lead: [name@yourdomain.com]
- DevOps: [devops@yourdomain.com]
- Documentation: `/INTEGRATION_GUIDE.md`
