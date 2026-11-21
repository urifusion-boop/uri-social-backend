"""
Permanent deletion of a user account, from the admin side.

Nothing of the account may remain. A fixed list of collections can't promise
that (192 collections today, more with every feature, and other services write
to databases on the same cluster), so this works from the data, not a list:

  1. Identity. Every id the account is known by: its user id, email and phone,
     its personal brand, the agencies it owns with all their brands, its agency
     memberships, workspaces, clients and SDK developer records.

  2. Delete. In EVERY collection of every database the backend can reach, each
     document whose owner field (user_id, brand_id, business_id, workspace_id,
     agency_id, email, ...) holds one of those ids is deleted.

  3. Scrub. Every document left anywhere is read in full, nested fields and
     text included. Any that still mentions the account is fixed:
       - the id is part of its key (_id)          -> deleted (a per-user record)
       - a shared record (a market's subscriber list, "decided_by" on a global
         topic, "ingested_by" on the strategy corpus, an access code it made)
                                                   -> the mention is replaced
                                                      with "deleted-account", or
                                                      removed from the list
     Payment records are handled by the chosen mode: deleted, or kept for the
     books with every personal detail taken out.

  4. Files. S3 objects whose path carries one of the ids, and objects the
     deleted records pointed at that nothing else still uses.

  5. Check. The same full read runs again and must find nothing.

Before deleting: running ad campaigns must be paused (pause_live_campaigns
does it on Meta/TikTok/Google), money left in an ad wallet must be refunded or
knowingly given up (the admin confirms the exact amount, which the audit record
keeps), and a platform admin must have admin revoked first.

Connected social accounts (direct Meta / Instagram / TikTok / LinkedIn / X /
Google; Outstand is no longer used) are cancelled on the platform where it
offers that, and every saved token is deleted with the rest.

A marker holding only a hash of the user id stays behind, so a session that was
still open is turned away instead of re-creating the account's brand on its
next request (see is_deleted_user / JWTBearer).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

DELETED = "deleted-account"
MARKERS = "deleted_account_markers"
JOBS = "account_deletions"

MODE_EVERYTHING = "everything"
MODE_KEEP_PAYMENTS = "keep_payments"
MODES = (MODE_EVERYTHING, MODE_KEEP_PAYMENTS)

_SYSTEM_DBS = {"admin", "local", "config"}
# Never touched: this feature's own bookkeeping (holds only hashes and the admin's id).
_OWN = {MARKERS, JOBS}
# Platform-wide settings: no document here belongs to a user, so none is ever
# deleted in step 2; step 3 still scrubs any mention.
_GLOBAL = {"subscription_tiers", "app_settings", "platform_settings"}

# A document whose one of these fields equals one of the account's ids belongs to it.
OWNER_ID_FIELDS = (
    "user_id", "userId", "owner_user_id", "owner_id", "member_user_id",
    "brand_id", "business_id", "workspace_id", "agency_id", "agency_member_id",
    "client_id", "developer_id", "sdk_client_id", "end_user_id",
    "metadata.brand_id", "metadata.user_id", "plan.business_id", "req.business_id",
)
OWNER_EMAIL_FIELDS = ("email", "user_email", "owner_email")

# Payment records: kept (anonymised) or deleted, depending on the mode.
PAYMENT_COLLECTIONS = {
    "payment_transactions", "credit_transactions", "jane_ads_transactions",
    "jane_ads_topups", "video_generation_billing", "brand_credit_usage",
    "inbox_usage_ledger", "inbox_billing_conversations", "invoices", "subscriptions",
}
# In a kept payment record, any field whose name says it's personal is blanked.
_PERSONAL_KEY = re.compile(
    r"(e_?mail|phone|mobile|msisdn|first_?name|last_?name|full_?name|customer_?name|"
    r"account_?name|card|authori[sz]ation|bank|bvn|address|ip_?address)", re.I)

_AREAS = (
    ("jane_ads_", "Ads"), ("ads_", "Ads"), ("inbox_", "Inbox"), ("mi_", "Market intelligence"),
    ("video_", "Video"), ("multi_clip", "Video"), ("zapcap", "Video"), ("submagic", "Video"),
    ("clip_", "Video"), ("content_", "Content"), ("blog_", "Content"), ("drafts", "Content"),
    ("brand_", "Brand & Playbook"), ("playbook", "Brand & Playbook"), ("writing_dna", "Brand & Playbook"),
    ("custom_", "Brand & Playbook"), ("visual_engine", "Content"), ("ai_image", "Content"),
    ("social_", "Connected accounts"), ("disconnected_", "Connected accounts"),
    ("whatsapp", "Connected accounts"), ("telegram", "Connected accounts"),
    ("linkedin_", "Connected accounts"), ("x_oauth", "Connected accounts"), ("pending_", "Connected accounts"),
    ("agenc", "Agency & team"), ("member_", "Agency & team"), ("workspace", "Agency & team"),
    ("clients", "Agency & team"), ("notification", "Notifications"), ("push_", "Notifications"),
    ("user_", "Account & billing"), ("users", "Account & billing"), ("credit", "Account & billing"),
    ("payment", "Account & billing"), ("access_code", "Account & billing"), ("magic_", "Account & billing"),
    ("api_keys", "Developer / API"), ("developers", "Developer / API"), ("sdk_", "Developer / API"),
)


def area_of(collection: str) -> str:
    c = collection.lower()
    for prefix, label in _AREAS:
        if c.startswith(prefix):
            return label
    return "Other"


def user_hash(user_id: str) -> str:
    return hashlib.sha256(f"uri-deleted-account:{user_id}".encode()).hexdigest()


# ── Identity ─────────────────────────────────────────────────────────────────

@dataclass
class Identity:
    user_id: str
    email: str = ""
    phone: str = ""
    names: list[str] = field(default_factory=list)      # only used inside kept payment records
    brand_ids: set[str] = field(default_factory=set)
    agency_ids: set[str] = field(default_factory=set)
    agency_member_ids: set[str] = field(default_factory=set)
    workspace_ids: set[str] = field(default_factory=set)
    client_ids: set[str] = field(default_factory=set)
    developer_ids: set[str] = field(default_factory=set)
    sdk_client_ids: set[str] = field(default_factory=set)
    # Brands of agencies this account belongs to but doesn't own. Records filed
    # under them belong to that agency: kept, with this account's name taken off.
    foreign_brand_ids: set[str] = field(default_factory=set)

    def ids(self) -> set[str]:
        out = {self.user_id} | self.brand_ids | self.agency_ids | self.agency_member_ids \
            | self.workspace_ids | self.client_ids | self.developer_ids | self.sdk_client_ids
        return {i for i in out if isinstance(i, str) and len(i) >= 8}

    def emails(self) -> set[str]:
        return {self.email, self.email.lower()} - {""}

    def to_doc(self) -> dict:
        return {k: (sorted(v) if isinstance(v, set) else v) for k, v in self.__dict__.items()}

    @classmethod
    def from_doc(cls, d: dict) -> "Identity":
        sets = {k for k, v in cls.__dataclass_fields__.items() if v.default_factory is set}  # type: ignore[attr-defined]
        return cls(**{k: (set(v) if k in sets else v) for k, v in d.items() if k in cls.__dataclass_fields__})

    def needle_regex(self) -> re.Pattern:
        """Matches any trace of the account inside any string."""
        parts = [re.escape(i) for i in sorted(self.ids(), key=len, reverse=True)]
        if self.email:
            parts.append("(?i:" + re.escape(self.email) + ")")
        digits = re.sub(r"\D", "", self.phone or "")
        if len(digits) >= 9:
            parts.append(re.escape(self.phone.strip()))
        return re.compile("|".join(parts))


async def resolve_identity(db, user: dict, databases: Optional[list] = None) -> Identity:
    """databases: every database to look in for the account's developer (API)
    records, which other services keep outside the main database."""
    from app.models.brand_account import BrandAccount

    uid = user.get("userId") or str(user.get("_id"))
    ident = Identity(
        user_id=uid,
        email=(user.get("email") or "").strip(),
        phone=(user.get("phone") or "").strip() if isinstance(user.get("phone"), str) else "",
        names=[n for n in (user.get("first_name"), user.get("last_name"),
                           f"{user.get('first_name', '')} {user.get('last_name', '')}".strip())
               if isinstance(n, str) and len(n.strip()) >= 2],
    )
    ident.brand_ids.add(BrandAccount.personal_brand_id(uid))
    async for b in db["brand_accounts"].find({"owner_user_id": uid, "agency_id": None}, {"brand_id": 1}):
        ident.brand_ids.add(b["brand_id"])
    async for a in db["agencies"].find({"owner_user_id": uid}, {"agency_id": 1}):
        ident.agency_ids.add(a["agency_id"])
    if ident.agency_ids:
        async for b in db["brand_accounts"].find({"agency_id": {"$in": list(ident.agency_ids)}}, {"brand_id": 1}):
            ident.brand_ids.add(b["brand_id"])
    member_q = {"$or": [{"user_id": uid}, {"agency_id": {"$in": list(ident.agency_ids)}}]}
    if ident.email:
        member_q["$or"].append({"email": {"$in": list(ident.emails())}})
    other_agencies = set()
    async for m in db["agency_members"].find(member_q, {"agency_member_id": 1, "agency_id": 1}):
        if m.get("agency_member_id"):
            ident.agency_member_ids.add(m["agency_member_id"])
        if m.get("agency_id") and m["agency_id"] not in ident.agency_ids:
            other_agencies.add(m["agency_id"])
    foreign_q: list[dict] = [{"owner_user_id": uid, "agency_id": {"$nin": [None] + sorted(ident.agency_ids)}}]
    if other_agencies:
        foreign_q.append({"agency_id": {"$in": sorted(other_agencies)}})
    async for b in db["brand_accounts"].find({"$or": foreign_q}, {"brand_id": 1}):
        if b["brand_id"] not in ident.brand_ids:
            ident.foreign_brand_ids.add(b["brand_id"])
    async for w in db["workspaces"].find(
            {"$or": [{"user_id": uid}, {"owner_user_id": uid}, {"created_by": uid}]}, {"workspace_id": 1, "_id": 1}):
        ident.workspace_ids.add(w.get("workspace_id") or str(w["_id"]))
    async for c in db["clients"].find({"owner_user_id": uid}, {"client_id": 1}):
        if c.get("client_id"):
            ident.client_ids.add(c["client_id"])
    for d_db in (databases or [db]):
        async for d in d_db["developers"].find(
                {"$or": [{"user_id": uid}, {"email": {"$in": sorted(ident.emails()) or [""]}}]}, {"developer_id": 1}):
            if d.get("developer_id"):
                ident.developer_ids.add(d["developer_id"])
        if ident.developer_ids:
            async for sc in d_db["sdk_client_profiles"].find({"developer_id": {"$in": sorted(ident.developer_ids)}},
                                                             {"sdk_client_id": 1}):
                if sc.get("sdk_client_id"):
                    ident.sdk_client_ids.add(sc["sdk_client_id"])
    return ident


def owner_filter(ident: Identity) -> dict:
    ids = sorted(ident.ids())
    ors: list[dict] = [{f: {"$in": ids}} for f in OWNER_ID_FIELDS]
    if ident.email:
        ors += [{f: {"$in": sorted(ident.emails())}} for f in OWNER_EMAIL_FIELDS]
    if not ident.foreign_brand_ids:
        return {"$or": ors}
    keep = sorted(ident.foreign_brand_ids)
    return {"$and": [{"$or": ors}] + [{f: {"$nin": keep}} for f in ("brand_id", "business_id", "workspace_id")]}


# ── Reading and rewriting documents ─────────────────────────────────────────

_REMOVE = object()


def contains(value: Any, rx: re.Pattern) -> bool:
    if isinstance(value, str):
        return bool(rx.search(value))
    if isinstance(value, dict):
        return any(contains(k, rx) or contains(v, rx) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains(v, rx) for v in value)
    return False


def scrub(value: Any, rx: re.Pattern, personal: bool = False, names: Iterable[str] = ()) -> Any:
    """A copy with every trace replaced. A list element that IS the account
    (a subscriber list, say) is removed rather than renamed; a dict key that
    carries an id is dropped with its value. personal=True also blanks fields
    whose name says they are personal and any exact match of the person's name
    (used only for payment records kept for the books)."""
    names = set(names)
    if isinstance(value, str):
        if rx.fullmatch(value):
            return DELETED
        out = rx.sub(DELETED, value)
        if personal and out in names:
            return DELETED
        return out
    if isinstance(value, dict):
        res = {}
        for k, v in value.items():
            if isinstance(k, str) and rx.search(k):
                continue
            if personal and isinstance(k, str) and _PERSONAL_KEY.search(k) and isinstance(v, (str, int, float)) \
                    and not isinstance(v, bool):
                res[k] = DELETED
                continue
            res[k] = scrub(v, rx, personal, names)
        return res
    if isinstance(value, list):
        out = []
        for v in value:
            if isinstance(v, str) and rx.fullmatch(v):
                continue                                   # the account itself: off the list
            out.append(scrub(v, rx, personal, names))
        return out
    return value


def bucket_keys(value: Any, bucket: str) -> set[str]:
    """S3 object keys of our media bucket that a document points at."""
    if not bucket:
        return set()
    rx = re.compile(r"https?://" + re.escape(bucket) + r"\.s3[.\-][a-z0-9\-.]*amazonaws\.com/([^\s\"'?#]+)"
                    r"|https?://s3[.\-][a-z0-9\-.]*amazonaws\.com/" + re.escape(bucket) + r"/([^\s\"'?#]+)")
    found: set[str] = set()

    def walk(v):
        if isinstance(v, str):
            if "amazonaws.com" in v:
                for a, b in rx.findall(v):
                    found.add(a or b)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(value)
    return found


# ── Where to look ────────────────────────────────────────────────────────────

async def reachable_databases() -> list:
    """Every database on every cluster the backend is connected to, main first."""
    from app import database as D
    from app.core.config import settings

    dbs, seen = [], set()
    clients = [D.client]
    sdk = getattr(D, "sdk_gateway_client", None)
    if sdk is not None and not _same_cluster(settings.MONGODB_URI, getattr(settings, "SDK_GATEWAY_MONGODB_URI", "")):
        clients.append(sdk)
    main_name = D.get_db().name
    for c in clients:
        if c is None:
            continue
        names = await c.list_database_names()
        for n in sorted(names, key=lambda x: (x != main_name, x)):
            if n in _SYSTEM_DBS or (id(c), n) in seen:
                continue
            seen.add((id(c), n))
            dbs.append(c[n])
    return dbs


def _same_cluster(a: str, b: str) -> bool:
    try:
        from pymongo.uri_parser import parse_uri
        return set(parse_uri(a)["nodelist"]) == set(parse_uri(b)["nodelist"])
    except Exception:
        return False


async def _collections(db) -> list[str]:
    names = await db.list_collection_names()
    return sorted(n for n in names if n not in _OWN and not n.startswith("system."))


# ── What must be settled first ──────────────────────────────────────────────

async def blockers(db, ident: Identity, user: dict, forfeit_wallet_ngn: Optional[float] = None) -> list[dict]:
    """What stops the deletion. Money in an ad wallet stops it unless the admin
    confirms giving up exactly that amount (forfeit_wallet_ngn)."""
    from app.agents.jane_ads.store import MongoWalletStore
    from app.agents.jane_ads.wallet import WalletService

    out: list[dict] = []
    if user.get("is_admin"):
        out.append({"kind": "admin", "message": "This is a platform admin. Revoke admin first."})
    brands = sorted(ident.brand_ids)
    live = []
    for coll, platform in (("jane_ads_meta_campaigns", "Meta"), ("jane_ads_tiktok_campaigns", "TikTok"),
                           ("jane_ads_google_campaigns", "Google")):
        async for c in db[coll].find({"brand_id": {"$in": brands}, "status": "active",
                                      "detached_at": {"$exists": False}},
                                     {"campaign_id": 1, "display_name": 1, "brand_id": 1}):
            live.append({"platform": platform, "campaign_id": c.get("campaign_id"),
                         "name": c.get("display_name") or "Campaign", "brand_id": c.get("brand_id")})
    if live:
        out.append({"kind": "live_campaigns", "items": live,
                    "message": f"{len(live)} ad campaign(s) still running. Pause them first."})
    wallet = WalletService(MongoWalletStore(db))
    money = []
    for b in brands:
        try:
            bal = await wallet.get_balance(b)
        except Exception:
            bal = 0.0
        if bal and bal > 0:
            money.append({"brand_id": b, "balance_ngn": round(bal, 2)})
    if money:
        total = round(sum(m["balance_ngn"] for m in money), 2)
        if forfeit_wallet_ngn is None or round(float(forfeit_wallet_ngn), 2) != total:
            out.append({"kind": "ad_wallet", "items": money, "total_ngn": total,
                        "message": f"₦{total:,.2f} is left in the ad wallet. Refund it to the customer, "
                                   "or confirm it will be lost, to continue."})
    return out


# ── Preview ──────────────────────────────────────────────────────────────────

async def preview(db, user: dict, databases: Optional[list] = None) -> dict:
    databases = databases if databases is not None else await reachable_databases()
    ident = await resolve_identity(db, user, databases)
    flt = owner_filter(ident)
    rows, payment_records = [], 0
    for d in databases:
        for coll in await _collections(d):
            if coll in _GLOBAL:
                continue
            n = await d[coll].count_documents(flt)
            if n:
                rows.append({"database": d.name, "collection": coll, "area": area_of(coll), "count": n})
                if coll in PAYMENT_COLLECTIONS:
                    payment_records += n
    areas: dict[str, int] = {}
    for r in rows:
        areas[r["area"]] = areas.get(r["area"], 0) + r["count"]
    agencies = []
    for aid in sorted(ident.agency_ids):
        a = await db["agencies"].find_one({"agency_id": aid}) or {}
        others = [m.get("email") or m.get("user_id") async for m in
                  db["agency_members"].find({"agency_id": aid, "user_id": {"$ne": ident.user_id}})]
        agencies.append({"agency_id": aid, "name": a.get("name"), "other_members": others})
    brands = [{"brand_id": b["brand_id"], "name": b.get("name")}
              async for b in db["brand_accounts"].find({"brand_id": {"$in": sorted(ident.brand_ids)}},
                                                       {"brand_id": 1, "name": 1})]
    return {
        "user_id": ident.user_id,
        "email": ident.email,
        "brands": brands,
        "agencies": agencies,
        "areas": [{"area": k, "count": v} for k, v in sorted(areas.items(), key=lambda kv: -kv[1])],
        "collections": sorted(rows, key=lambda r: (r["area"], -r["count"])),
        "records_total": sum(r["count"] for r in rows),
        "payment_records": payment_records,
        "connected_accounts": await db["social_connections"].count_documents(
            {"$or": [{"user_id": ident.user_id}, {"brand_id": {"$in": sorted(ident.brand_ids)}}]}),
        "blockers": await blockers(db, ident, user),
    }


# ── Deleting ─────────────────────────────────────────────────────────────────

class Deps:
    """The outside world, swappable in tests."""

    def __init__(self, databases=None, bucket: Optional[str] = None, s3=None, revoke=None):
        self._databases = databases
        self.bucket = bucket
        self.s3 = s3
        self.revoke = revoke

    async def databases(self):
        return self._databases if self._databases is not None else await reachable_databases()

    def media_bucket(self) -> str:
        if self.bucket is not None:
            return self.bucket
        import os
        return os.environ.get("S3_MEDIA_BUCKET", "")

    def s3_client(self):
        if self.s3 is not None:
            return self.s3
        from app.utils import s3_upload
        return s3_upload._s3_client

    async def revoke_access(self, conn: dict) -> str:
        if self.revoke is not None:
            return await self.revoke(conn)
        return await revoke_platform_access(conn)


async def start_job(db, user: dict, mode: str, admin_user_id: str, forfeited_wallet_ngn: float = 0.0,
                    databases: Optional[list] = None) -> str:
    databases = databases if databases is not None else await reachable_databases()
    ident = await resolve_identity(db, user, databases)
    job_id = "del_" + uuid.uuid4().hex[:16]
    await db[JOBS].insert_one({
        "job_id": job_id, "user_hash": user_hash(ident.user_id), "mode": mode,
        "admin_user_id": admin_user_id, "status": "running", "progress": "Starting",
        "forfeited_wallet_ngn": round(float(forfeited_wallet_ngn or 0), 2),
        "started_at": datetime.now(timezone.utc),
        # Needed to finish (or resume) after the user record itself is gone;
        # removed the moment the job completes.
        "_identity": ident.to_doc(),
    })
    return job_id


async def run_job(db, job_id: str, deps: Optional[Deps] = None) -> dict:
    deps = deps or Deps()
    job = await db[JOBS].find_one({"job_id": job_id})
    if not job or not job.get("_identity"):
        raise ValueError("Unknown or finished deletion job")
    ident = Identity.from_doc(job["_identity"])
    mode = job["mode"]
    rx = ident.needle_regex()
    bucket = deps.media_bucket()
    report: dict[str, Any] = {"deleted": {}, "scrubbed": {}, "anonymised": {}, "errors": []}

    async def progress(text: str):
        await db[JOBS].update_one({"job_id": job_id}, {"$set": {"progress": text}})

    try:
        # Sessions still open are refused from now on (JWTBearer).
        await db[MARKERS].update_one({"user_hash": user_hash(ident.user_id)},
                                     {"$setOnInsert": {"user_hash": user_hash(ident.user_id),
                                                       "deleted_at": datetime.now(timezone.utc)}},
                                     upsert=True)
        _deleted_cache["at"] = 0.0

        # Connected accounts: Uri's access is cancelled on the platform where it
        # offers that; the saved tokens themselves go with the rest in step 2.
        await progress("Cancelling Uri's access to connected accounts")
        access: dict[str, int] = {}
        async for c in db["social_connections"].find(
                {"$or": [{"user_id": ident.user_id}, {"brand_id": {"$in": sorted(ident.brand_ids)}}]}):
            try:
                outcome = await deps.revoke_access(c)
            except Exception as e:                      # reported; the token is deleted regardless
                outcome = "failed"
                report["errors"].append(f"Cancel access ({c.get('platform')}): {str(e)[:120]}")
            key = f"{c.get('platform') or 'unknown'}: {outcome}"
            access[key] = access.get(key, 0) + 1
        report["connected_accounts"] = access

        databases = await deps.databases()
        flt = owner_filter(ident)
        candidate_keys: set[str] = set()

        # Step 2: everything the account owns.
        for i, d in enumerate(databases):
            for coll in await _collections(d):
                if coll in _GLOBAL or (mode == MODE_KEEP_PAYMENTS and coll in PAYMENT_COLLECTIONS):
                    continue
                await progress(f"Deleting records: {d.name}.{coll}")
                async for doc in d[coll].find(flt):
                    candidate_keys |= bucket_keys(doc, bucket)
                res = await d[coll].delete_many(flt)
                if res.deleted_count:
                    report["deleted"][f"{d.name}.{coll}"] = res.deleted_count

        # Step 3: every remaining trace, anywhere.
        keep_keys = await _sweep(db, databases, ident, rx, mode, bucket, candidate_keys, report, progress, fix=True)

        # Step 4: files.
        await progress("Deleting files")
        report["files"] = await _delete_files(deps, bucket, ident, candidate_keys - keep_keys, report)

        # Step 5: check.
        await progress("Checking nothing is left")
        check: dict[str, Any] = {"deleted": {}, "scrubbed": {}, "anonymised": {}, "errors": [], "remaining": {}}
        await _sweep(db, databases, ident, rx, mode, bucket, set(), check, None, fix=False)
        report["remaining"] = check["remaining"]

        status = "done" if not report["remaining"] else "done_with_leftovers"
        await db[JOBS].update_one({"job_id": job_id}, {
            "$set": {"status": status, "progress": "Finished", "finished_at": datetime.now(timezone.utc),
                     "report": _report_for_storage(report)},
            "$unset": {"_identity": ""},
        })
        return report
    except Exception as e:
        await db[JOBS].update_one({"job_id": job_id}, {"$set": {
            "status": "failed", "error": str(e)[:300], "report": _report_for_storage(report)}})
        raise


async def _sweep(db, databases, ident: Identity, rx: re.Pattern, mode: str, bucket: str,
                 candidate_keys: set[str], report: dict, progress, fix: bool) -> set[str]:
    """Reads every document of every collection. fix=True repairs what it finds;
    fix=False only counts it (report["remaining"]). Returns the candidate file
    keys that some surviving document still points at."""
    keep: set[str] = set()
    for d in databases:
        for coll in await _collections(d):
            if progress:
                await progress(f"Checking every record: {d.name}.{coll}")
            label = f"{d.name}.{coll}"
            async for doc in d[coll].find({}):
                if not contains(doc, rx):
                    if candidate_keys:
                        keep |= bucket_keys(doc, bucket) & candidate_keys
                    continue
                if not fix:
                    report["remaining"][label] = report["remaining"].get(label, 0) + 1
                    continue
                is_payment = coll in PAYMENT_COLLECTIONS
                if contains(doc.get("_id"), rx) or (is_payment and mode == MODE_EVERYTHING):
                    candidate_keys |= bucket_keys(doc, bucket)
                    await d[coll].delete_one({"_id": doc["_id"]})
                    report["deleted"][label] = report["deleted"].get(label, 0) + 1
                    continue
                personal = is_payment and mode == MODE_KEEP_PAYMENTS
                fixed = scrub({k: v for k, v in doc.items() if k != "_id"}, rx, personal=personal, names=ident.names)
                await d[coll].replace_one({"_id": doc["_id"]}, fixed)
                bucket_name = "anonymised" if personal else "scrubbed"
                report[bucket_name][label] = report[bucket_name].get(label, 0) + 1
                if candidate_keys:
                    keep |= bucket_keys(fixed, bucket) & candidate_keys
    return keep


async def _delete_files(deps: Deps, bucket: str, ident: Identity, referenced: set[str], report: dict) -> dict:
    if not bucket:
        return {"deleted": 0, "note": "no media bucket configured"}
    s3 = deps.s3_client()
    ids = sorted(ident.ids(), key=len, reverse=True)
    keys = set(referenced)
    loop = asyncio.get_running_loop()
    try:
        token = None
        while True:
            kw = {"Bucket": bucket, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = await loop.run_in_executor(None, lambda: s3.list_objects_v2(**kw))
            for o in page.get("Contents", []) or []:
                if any(i in o["Key"] for i in ids):
                    keys.add(o["Key"])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    except Exception as e:
        report["errors"].append(f"Listing files: {str(e)[:160]}")
    deleted = 0
    batch = sorted(keys)
    for i in range(0, len(batch), 1000):
        chunk = [{"Key": k} for k in batch[i:i + 1000]]
        try:
            res = await loop.run_in_executor(
                None, lambda: s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True}))
            errs = res.get("Errors") or []
            deleted += len(chunk) - len(errs)
            for e in errs[:5]:
                report["errors"].append(f"Deleting file: {e.get('Code')} {e.get('Message', '')[:80]}")
        except Exception as e:
            report["errors"].append(f"Deleting files: {str(e)[:160]}")
    return {"deleted": deleted, "found": len(keys)}


def _report_for_storage(report: dict) -> dict:
    # Collection names and counts only: nothing that identifies the person.
    return {k: v for k, v in report.items()}


# ── Sessions of deleted accounts ─────────────────────────────────────────────

_deleted_cache: dict[str, Any] = {"at": 0.0, "hashes": set()}
_CACHE_SECONDS = 60


async def is_deleted_user(user_id: Optional[str]) -> bool:
    if not user_id:
        return False
    now = time.monotonic()
    if now - _deleted_cache["at"] > _CACHE_SECONDS:
        try:
            from app.database import get_db
            db = get_db()
            _deleted_cache["hashes"] = {m["user_hash"] async for m in db[MARKERS].find({}, {"user_hash": 1})}
            _deleted_cache["at"] = now
        except Exception:
            return False
    return user_hash(user_id) in _deleted_cache["hashes"]


# ── Platform access ──────────────────────────────────────────────────────────

async def revoke_platform_access(conn: dict) -> str:
    """Cancel Uri's access to one connected account on the platform itself.
    Returns "cancelled", or "token deleted" where the platform has no cancel
    call for that kind of token (Facebook Page tokens, X); the saved token is
    deleted with the account's other records either way."""
    import httpx
    from app.core.config import settings

    platform = (conn.get("platform") or "").lower()
    async with httpx.AsyncClient(timeout=10) as http:
        user_token = conn.get("user_access_token")
        if user_token and platform in ("facebook", "instagram", "meta", "meta_ads", "facebook_ads"):
            r = await http.delete(f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/me/permissions",
                                  params={"access_token": user_token})
            r.raise_for_status()
            return "cancelled"
        if platform == "tiktok" and conn.get("access_token") and settings.TIKTOK_APP_CLIENT_KEY:
            r = await http.post("https://open.tiktokapis.com/v2/oauth/revoke/",
                                data={"client_key": settings.TIKTOK_APP_CLIENT_KEY,
                                      "client_secret": settings.TIKTOK_APP_CLIENT_SECRET,
                                      "token": conn["access_token"]})
            r.raise_for_status()
            return "cancelled"
        if conn.get("linkedin_access_token") and settings.LINKEDIN_CLIENT_ID:
            r = await http.post("https://www.linkedin.com/oauth/v2/revoke",
                                data={"client_id": settings.LINKEDIN_CLIENT_ID,
                                      "client_secret": settings.LINKEDIN_CLIENT_SECRET,
                                      "token": conn["linkedin_access_token"]})
            r.raise_for_status()
            return "cancelled"
        if platform.startswith("google") and conn.get("refresh_token"):
            r = await http.post("https://oauth2.googleapis.com/revoke", data={"token": conn["refresh_token"]})
            if r.status_code not in (200, 400):        # 400 = already invalid
                r.raise_for_status()
            return "cancelled"
    return "token deleted"


# ── Running ads ──────────────────────────────────────────────────────────────

async def pause_live_campaigns(db, user: dict) -> dict:
    """Pause every running campaign of the account's brands on the platform, the
    same calls the campaign list's own pause button makes."""
    from app.core.config import settings
    from app.agents.jane_ads import google_flow
    from app.agents.jane_ads.adapters.meta import MetaAdPlatformAdapter
    from app.agents.jane_ads.adapters.tiktok import TikTokAdsAdapter

    ident = await resolve_identity(db, user)
    found = next((b for b in await blockers(db, ident, user) if b["kind"] == "live_campaigns"), None)
    paused, failed = [], []
    for item in (found or {}).get("items", []):
        cid, platform = item["campaign_id"], item["platform"]
        try:
            if platform == "Google":
                await google_flow.GoogleCampaigns(db).set_delivery(cid, False)
                coll = google_flow.GOOGLE_CAMPAIGNS
            elif platform == "TikTok":
                await TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID,
                                       access_token=settings.TIKTOK_ADS_ACCESS_TOKEN).set_delivery(cid, False)
                coll = "jane_ads_tiktok_campaigns"
            else:
                await MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN).set_delivery(cid, False)
                coll = "jane_ads_meta_campaigns"
            await db[coll].update_one({"campaign_id": cid}, {"$set": {"status": "paused"}})
            paused.append(item)
        except Exception as e:
            failed.append({**item, "error": str(e)[:200]})
    return {"paused": paused, "failed": failed}
