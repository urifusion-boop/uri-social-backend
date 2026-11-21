"""Admin "delete account": nothing of the account remains, nothing of anyone else is touched."""
import pytest

from app.services import account_deletion as AD

from tests.test_jane_ads_growth import mongomock_motor, run  # noqa: F401

A = "aaaaaaaa-1111-2222-3333-444444444444"          # the account being deleted
B = "bbbbbbbb-1111-2222-3333-444444444444"          # a member of A's agency
C = "cccccccc-1111-2222-3333-444444444444"          # an unrelated customer
A_EMAIL, C_EMAIL = "Owner.A@Example.com", "c@example.com"
A_BRAND = "brnd_personal_" + A
A_AGENCY, A_AGENCY_BRAND = "agcy_Aq81kZpL0x", "brnd_agencyA_client"
D_AGENCY_BRAND = "brnd_in_someone_elses_agency"
BUCKET = "uri-media-test"
URL = f"https://{BUCKET}.s3.eu-west-1.amazonaws.com/"


class FakeS3:
    def __init__(self, keys):
        self.keys = set(keys)
        self.deleted = set()

    def list_objects_v2(self, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.keys)], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.add(o["Key"])
            self.keys.discard(o["Key"])
        return {}


def seed():
    client = mongomock_motor.AsyncMongoMockClient()
    db, gw = client["urisocial"], client["gateway"]

    async def go():
        await db.users.insert_many([
            {"userId": A, "email": A_EMAIL, "phone": "+2348031234567", "first_name": "Ada", "last_name": "Obi"},
            {"userId": B, "email": "b@example.com"},
            {"userId": C, "email": C_EMAIL},
        ])
        await db.brand_accounts.insert_many([
            {"brand_id": A_BRAND, "owner_user_id": A, "agency_id": None, "name": "A personal"},
            {"brand_id": A_AGENCY_BRAND, "owner_user_id": A, "agency_id": A_AGENCY, "name": "A's client"},
            {"brand_id": D_AGENCY_BRAND, "owner_user_id": A, "agency_id": "agcy_D", "name": "D's client A made"},
            {"brand_id": "brnd_personal_" + C, "owner_user_id": C, "agency_id": None, "name": "C personal"},
        ])
        await db.agencies.insert_many([{"agency_id": A_AGENCY, "owner_user_id": A, "name": "A Agency"},
                                       {"agency_id": "agcy_D", "owner_user_id": C, "name": "C Agency"}])
        await db.agency_members.insert_many([
            {"agency_member_id": "ambr_A_7Hq2xP", "agency_id": A_AGENCY, "user_id": A, "role": "admin"},
            {"agency_member_id": "ambr_B_9Kd3vQ", "agency_id": A_AGENCY, "user_id": B, "role": "agent"},
            {"agency_member_id": "ambr_C_2Lm8wR", "agency_id": "agcy_D", "user_id": C, "role": "admin"},
        ])
        await db.member_brand_access.insert_one({"agency_member_id": "ambr_B_9Kd3vQ", "brand_id": A_AGENCY_BRAND})
        await db.content_drafts.insert_many([
            {"user_id": A, "brand_id": A_BRAND, "image_url": URL + "uri-social/content-drafts/x1.png"},
            {"user_id": C, "brand_id": "brnd_personal_" + C, "image_url": URL + "uri-social/content-drafts/shared.png"},
        ])
        await db.jane_ads_wizard_drafts.insert_one({"brand_id": A_AGENCY_BRAND, "draft_id": "d1", "payload": {}})
        # A worked inside C's agency too: that brand's records are the agency's, not A's
        await db.content_drafts.insert_one({"user_id": A, "brand_id": D_AGENCY_BRAND, "caption": "for D"})
        await db.notifications.insert_one({"user_id": B, "metadata": {"brand_id": A_BRAND, "message": "hi"}})
        await db.payment_transactions.insert_many([
            {"user_id": A, "amount": 30000, "reference": "ref1",
             "squad_response": {"Body": {"email": A_EMAIL, "customer_name": "Ada Obi", "amount": 30000}}},
            {"user_id": C, "amount": 45000, "reference": "ref2", "squad_response": {"Body": {"email": C_EMAIL}}},
        ])
        await db.mi_markets.insert_one({"market": "lagos", "subscribers": [A, C]})
        await db.mi_market_topics.insert_one({"topic": "fashion", "decided_by": A})
        await db.jane_ads_strategies.insert_one({"name": "global", "ingested_by": A})
        await db.notification_daily_claims.insert_one({"_id": f"{A}:2026-11-20", "user_id": A})
        await db.access_codes.insert_one({"code": "X1", "created_by": A, "redeemed": 3})
        await db.brand_profiles.insert_one({"brand_id": D_AGENCY_BRAND, "brand_name": "D client",
                                            "notes": f"set up by {A_EMAIL}"})
        await db.social_connections.insert_many([
            {"user_id": A, "brand_id": A_BRAND, "platform": "linkedin", "linkedin_access_token": "tok"},
            {"user_id": C, "brand_id": "brnd_personal_" + C, "platform": "facebook", "page_access_token": "t2"},
        ])
        await db.subscription_tiers.insert_one({"tier_id": "starter", "note": "global"})
        await gw.developers.insert_one({"developer_id": "devr_A_5Tn6yU", "email": A_EMAIL.lower()})
        await gw.api_keys.insert_one({"developer_id": "devr_A_5Tn6yU", "key_hash": "h"})
        await gw.developers.insert_one({"developer_id": "devr_C_8Pq1zS", "email": C_EMAIL})
    run(go())
    return db, gw


def everything_left(dbs, needle):
    import re
    rx = re.compile(re.escape(needle), re.I)
    left = []

    async def go():
        for d in dbs:
            for coll in await d.list_collection_names():
                if coll in (AD.JOBS, AD.MARKERS):
                    continue
                async for doc in d[coll].find({}):
                    if AD.contains(doc, rx):
                        left.append((coll, doc))
    run(go())
    return left


def delete(db, gw, mode, s3=None, revoked=None):
    user = run(db.users.find_one({"userId": A}))
    job = run(AD.start_job(db, user, mode, "admin-1", databases=[db, gw]))

    async def revoke(conn):
        (revoked if revoked is not None else []).append(conn.get("platform"))
        return "cancelled"
    deps = AD.Deps(databases=[db, gw], bucket=BUCKET, s3=s3 or FakeS3([]), revoke=revoke)
    report = run(AD.run_job(db, job, deps))
    return job, report


def test_nothing_of_the_account_remains_anywhere():
    db, gw = seed()
    s3 = FakeS3([f"uri-social/user-uploads/{A}/logo.png", "uri-social/content-drafts/x1.png",
                 "uri-social/content-drafts/shared.png", "uri-social/ad-styles/SEED-074.jpg"])
    revoked = []
    job, report = delete(db, gw, AD.MODE_EVERYTHING, s3, revoked)
    for needle in (A, A_EMAIL, A_BRAND, A_AGENCY, A_AGENCY_BRAND, "ambr_A_7Hq2xP", "devr_A_5Tn6yU", "+2348031234567"):
        assert everything_left([db, gw], needle) == [], needle
    assert report["remaining"] == {}
    job_doc = run(db[AD.JOBS].find_one({"job_id": job}))
    assert job_doc["status"] == "done" and "_identity" not in job_doc
    assert A not in str(job_doc) and A_EMAIL.lower() not in str(job_doc).lower()
    # files: the user's folder and the image only A's draft used; shared and global files stay
    assert s3.deleted == {f"uri-social/user-uploads/{A}/logo.png", "uri-social/content-drafts/x1.png"}
    assert revoked == ["linkedin"]


def test_everyone_else_is_untouched():
    db, gw = seed()
    delete(db, gw, AD.MODE_EVERYTHING)

    async def check():
        assert await db.users.count_documents({"userId": {"$in": [B, C]}}) == 2
        assert await db.content_drafts.count_documents({"user_id": C}) == 1
        assert await db.payment_transactions.count_documents({"user_id": C}) == 1
        assert await db.social_connections.count_documents({"user_id": C}) == 1
        assert await db.agency_members.count_documents({"user_id": C}) == 1
        assert await db.subscription_tiers.count_documents({}) == 1
        assert await gw.developers.count_documents({"developer_id": "devr_C_8Pq1zS"}) == 1
        # A's agency goes with A: B's membership and access to its brands go too, B stays
        assert await db.agency_members.count_documents({"user_id": B}) == 0
        assert await db.member_brand_access.count_documents({}) == 0
        assert await db.agencies.count_documents({"agency_id": A_AGENCY}) == 0
        assert await db.agency_members.count_documents({"agency_id": "agcy_D"}) == 1
        # a brand A made inside C's agency belongs to C's agency: kept, A's name off it
        kept = await db.content_drafts.find_one({"brand_id": D_AGENCY_BRAND})
        assert kept and kept["user_id"] == AD.DELETED and kept["caption"] == "for D"
        d = await db.brand_accounts.find_one({"brand_id": D_AGENCY_BRAND})
        assert d and d["owner_user_id"] == AD.DELETED
        assert (await db.brand_profiles.find_one({"brand_id": D_AGENCY_BRAND}))["notes"] == f"set up by {AD.DELETED}"
        # shared records keep existing, only A is taken out
        assert (await db.mi_markets.find_one({"market": "lagos"}))["subscribers"] == [C]
        assert (await db.mi_market_topics.find_one({"topic": "fashion"}))["decided_by"] == AD.DELETED
        assert (await db.jane_ads_strategies.find_one({"name": "global"}))["ingested_by"] == AD.DELETED
        assert (await db.access_codes.find_one({"code": "X1"}))["redeemed"] == 3
    run(check())


def test_delete_everything_removes_payment_records():
    db, gw = seed()
    delete(db, gw, AD.MODE_EVERYTHING)
    assert run(db.payment_transactions.count_documents({"reference": "ref1"})) == 0


def test_keep_payments_keeps_the_amounts_without_the_person():
    db, gw = seed()
    _, report = delete(db, gw, AD.MODE_KEEP_PAYMENTS)
    kept = run(db.payment_transactions.find_one({"reference": "ref1"}))
    assert kept["amount"] == 30000 and kept["squad_response"]["Body"]["amount"] == 30000
    assert kept["user_id"] == AD.DELETED
    assert kept["squad_response"]["Body"]["email"] == AD.DELETED
    assert kept["squad_response"]["Body"]["customer_name"] == AD.DELETED
    assert report["remaining"] == {}
    assert everything_left([db, gw], A) == [] and everything_left([db, gw], A_EMAIL) == []


def test_running_ads_and_wallet_money_block_until_settled():
    db, gw = seed()
    user = run(db.users.find_one({"userId": A}))

    async def go():
        await db.jane_ads_meta_campaigns.insert_one({"brand_id": A_BRAND, "campaign_id": "c1", "status": "active"})
        await db.jane_ads_wallets.insert_one({"business_id": A_AGENCY_BRAND, "balance_ngn": 5000.0})
        ident = await AD.resolve_identity(db, user)
        kinds = {b["kind"] for b in await AD.blockers(db, ident, user)}
        assert kinds == {"live_campaigns", "ad_wallet"}
        await db.jane_ads_meta_campaigns.update_one({"campaign_id": "c1"}, {"$set": {"status": "paused"}})
        # the wrong amount doesn't count as confirming the loss; the exact amount does
        assert {b["kind"] for b in await AD.blockers(db, ident, user, forfeit_wallet_ngn=4000)} == {"ad_wallet"}
        assert await AD.blockers(db, ident, user, forfeit_wallet_ngn=5000) == []
    run(go())


def test_platform_admin_must_be_revoked_first():
    db, _ = seed()
    run(db.users.update_one({"userId": A}, {"$set": {"is_admin": True}}))
    user = run(db.users.find_one({"userId": A}))
    ident = run(AD.resolve_identity(db, user))
    assert [b["kind"] for b in run(AD.blockers(db, ident, user))] == ["admin"]


def test_preview_lists_what_will_go():
    db, gw = seed()
    user = run(db.users.find_one({"userId": A}))
    p = run(AD.preview(db, user, databases=[db, gw]))
    colls = {r["collection"] for r in p["collections"]}
    assert {"users", "content_drafts", "payment_transactions", "agencies", "developers", "api_keys"} <= colls
    assert p["payment_records"] == 1
    assert p["agencies"][0]["other_members"] == [B]
    assert {b["brand_id"] for b in p["brands"]} == {A_BRAND, A_AGENCY_BRAND}
    assert p["blockers"] == []


def test_open_sessions_of_a_deleted_account_are_refused(monkeypatch):
    db, gw = seed()
    import app.database as D
    monkeypatch.setattr(D, "get_db", lambda: db)
    AD._deleted_cache["at"] = 0.0
    assert run(AD.is_deleted_user(A)) is False
    delete(db, gw, AD.MODE_EVERYTHING)
    AD._deleted_cache["at"] = 0.0
    assert run(AD.is_deleted_user(A)) is True
    assert run(AD.is_deleted_user(C)) is False
    # the marker holds a hash, not the id
    assert A not in str(run(db[AD.MARKERS].find_one({})))
