"""
Seed performance data for a user account.

Seeds realistic content_drafts + content_analytics so the
/content-calendar/performance endpoint returns real metrics.

Usage:
    python scripts/seed_performance_data.py
    python scripts/seed_performance_data.py --user-id <uuid> --clear
"""

import argparse
import random
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

# ── Config ────────────────────────────────────────────────────────────────────
MONGODB_URI = "mongodb://urifusion:UriTest2024%21@4.221.74.63:27018/Uri_Insight?authSource=admin"
MONGODB_DB  = "Uri_Insight"
USER_ID     = "401f0801-a2f9-44da-8fd1-a0dd69eebcdd"  # shorekoya@gmail.com

# ── Seed content ──────────────────────────────────────────────────────────────
POSTS = [
    # (content_snippet, has_image, platform, days_ago, likes, comments, shares, impressions)
    ("5 investment tips every entrepreneur should know about savings and profit",        True,  "linkedin",  3,  120, 18, 25, 3200),
    ("How to grow your finance business using smart money management strategies",        True,  "instagram", 5,   98, 12, 19, 2800),
    ("Beginner's guide to personal finance for small business owners",                   False, "linkedin",  7,   75,  9, 14, 1900),
    ("What nobody tells you about investment and revenue growth in 2026",                True,  "instagram", 9,  145, 22, 31, 4100),
    ("The truth about savings strategies in the finance industry",                       True,  "linkedin", 12,   88, 11, 17, 2300),
    ("5 mistakes every entrepreneur makes with their budget and cost planning",          False, "linkedin", 14,   62,  7, 10, 1600),
    ("How to use marketing and brand strategy to grow your audience",                    True,  "instagram",16,  110, 15, 20, 3000),
    ("Beginner's guide to content marketing for Nigerian business owners",               True,  "linkedin", 18,   95, 13, 18, 2600),
    ("5 things every startup founder should know about digital marketing campaigns",     False, "instagram",20,   70,  8, 12, 1800),
    ("Why social media engagement matters for your brand and business growth",           True,  "linkedin", 22,  130, 20, 28, 3500),
    ("How to learn new skills and how to grow through professional development",         True,  "instagram",24,   55,  6,  9, 1400),
    ("5 ways to improve your sales strategy and customer acquisition process",           True,  "linkedin", 26,  105, 14, 22, 2900),
    ("The biggest business trends in 2026 for entrepreneurs and startup founders",       False, "instagram",28,   80, 10, 15, 2100),
    ("Is investment in tech tools worth it? Here's what we found about ROI",             True,  "linkedin", 30,  118, 17, 24, 3100),
    ("Behind the story: How we grew our business revenue using automation tools",        True,  "instagram",32,   92, 12, 16, 2400),
    ("5 finance mistakes that cost Nigerian entrepreneurs their profit margins",         True,  "linkedin", 35,  140, 21, 30, 3800),
    ("How to build a strong marketing brand that drives consistent sales",               False, "instagram",38,   68,  8, 11, 1700),
    ("What nobody tells you about growing a business with limited budget",               True,  "linkedin", 40,  115, 16, 23, 3050),
    ("Beginner's guide to investment strategies for first-time entrepreneurs",           True,  "instagram",42,   88, 11, 18, 2350),
    ("5 tips for better audience engagement on your social media platforms",             True,  "linkedin", 45,  102, 14, 20, 2750),
]


def seed(user_id: str, clear: bool = False):
    client = MongoClient(MONGODB_URI)
    db = client[MONGODB_DB]

    if clear:
        deleted_d = db["content_drafts"].delete_many({"user_id": user_id, "status": "published", "_seeded": True})
        deleted_a = db["content_analytics"].delete_many({"_seeded": True, "user_id": user_id})
        print(f"🗑️  Cleared {deleted_d.deleted_count} drafts, {deleted_a.deleted_count} analytics records")

    now = datetime.now(timezone.utc)
    draft_docs = []
    analytics_docs = []

    for content, has_image, platform, days_ago, likes, comments, shares, impressions in POSTS:
        draft_id = str(uuid.uuid4())
        published_at = now - timedelta(days=days_ago)

        # Add slight randomness so data feels real
        likes       = max(0, likes       + random.randint(-10, 10))
        comments    = max(0, comments    + random.randint(-3,  3))
        shares      = max(0, shares      + random.randint(-5,  5))
        impressions = max(100, impressions + random.randint(-200, 200))

        draft_docs.append({
            "id":             draft_id,
            "request_id":     str(uuid.uuid4()),
            "user_id":        user_id,
            "content":        content,
            "platform":       platform,
            "platforms":      [platform],
            "has_image":      has_image,
            "status":         "published",
            "published_date": published_at.isoformat(),
            "created_at":     published_at.isoformat(),
            "_seeded":        True,
        })

        analytics_docs.append({
            "draft_id":    draft_id,
            "user_id":     user_id,
            "platform":    platform,
            "likes":       likes,
            "comments":    comments,
            "shares":      shares,
            "impressions": impressions,
            "recorded_at": published_at.isoformat(),
            "_seeded":     True,
        })

    db["content_drafts"].insert_many(draft_docs)
    db["content_analytics"].insert_many(analytics_docs)

    print(f"✅ Seeded {len(draft_docs)} posts + {len(analytics_docs)} analytics records for user {user_id}")
    print()

    # Quick summary
    total_eng = sum(
        (l + c + s) / i * 100
        for _, _, _, _, l, c, s, i in POSTS
    ) / len(POSTS)
    print(f"📊 Expected performance output:")
    print(f"   post_count:        {len(POSTS)}")
    print(f"   avg_engagement:    ~{total_eng:.1f}%")
    print(f"   top formats:       image, text")
    print(f"   top topics:        finance, marketing, business, education")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default=USER_ID)
    parser.add_argument("--clear",   action="store_true", help="Delete previously seeded data first")
    args = parser.parse_args()
    seed(args.user_id, args.clear)
