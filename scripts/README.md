# Database Scripts

This directory contains database maintenance and seeding scripts for URI Social backend.

## Subscription Tier Seeding

### Overview
The `seed_subscription_tiers.py` script initializes or updates subscription tiers with multi-duration pricing support (PRD: Subscription Plan Upgrade with 5% Bulk Discount).

### Features
- Creates new subscription tiers if they don't exist
- Updates existing tiers with multi-duration pricing fields
- Applies 5% discount formula: `(monthly_price × months) × 0.95`
- Maintains backward compatibility with legacy `price_ngn` and `credits` fields

### Tiers Configured
1. **Starter Plan**: ₦15,000/month (20 credits)
2. **Growth Plan**: ₦25,000/month (35 credits)
3. **Pro Plan**: ₦40,000/month (50 credits)
4. **Agency Plan**: ₦80,000/month (100 credits)
5. **Custom Plan**: ₦750 per credit (pay-as-you-go)

### Multi-Duration Pricing
All tiers (except Custom) support:
- **Monthly**: No discount
- **3 Months**: 5% discount on total
- **6 Months**: 5% discount on total
- **12 Months**: 5% discount on total

**Example: Starter Plan**
- Monthly: ₦15,000
- 3-month: ₦42,750 (₦45,000 - 5% = ₦42,750)
- 6-month: ₦85,500 (₦90,000 - 5% = ₦85,500)
- 12-month: ₦171,000 (₦180,000 - 5% = ₦171,000)

### Usage

#### Prerequisites
- MongoDB connection configured in `.env` or environment variables
- Python 3.8+
- Required packages: `motor`, `pydantic`

#### Run the script

From the project root directory:

```bash
# Activate virtual environment (if using one)
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows

# Run the seed script
python scripts/seed_subscription_tiers.py
```

#### Expected Output
```
🌱 Starting subscription tier seeding...
📡 Connecting to MongoDB: mongodb://...
✅ Updated tier: starter - Starter Plan
   Monthly: ₦15,000
   3-month: ₦42,750 (save 5%)
   6-month: ₦85,500 (save 5%)
   12-month: ₦171,000 (save 5%)
...
🎉 Seeding complete!
   Created: 0 new tier(s)
   Updated: 5 existing tier(s)
   Total: 5 tier(s) in database
```

### When to Run
- **Initial setup**: First time setting up the database
- **After PRD changes**: When tier pricing or features are updated
- **Production deployment**: After deploying multi-duration subscription feature
- **Data migration**: Converting legacy tiers to new schema

### Important Notes
- ✅ **Safe to run multiple times**: Script uses upsert logic (update if exists, create if not)
- ✅ **Preserves existing data**: Only updates pricing fields, keeps other tier data intact
- ✅ **Backward compatible**: Maintains legacy `price_ngn` and `credits` fields
- ⚠️ **Production**: Review changes before running in production
- ⚠️ **Backup**: Consider backing up the `subscription_tiers` collection first

### Troubleshooting

**Connection Error**
```
Error: Could not connect to MongoDB
```
Solution: Check `MONGODB_URI` in `.env` file

**Permission Error**
```
PermissionError: [Errno 13] Permission denied
```
Solution: Make script executable: `chmod +x scripts/seed_subscription_tiers.py`

**Import Error**
```
ModuleNotFoundError: No module named 'motor'
```
Solution: Install dependencies: `pip install motor pydantic`
