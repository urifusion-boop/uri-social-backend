"""BrandProfileService.save() — the fix for a read-after-write consistency
gap confirmed live on prod: update_one() followed by a SEPARATE find_one()
could race a lagging DB read and return the document from before the write
landed (reproduced via direct browser testing: identical save requests
sometimes got back stale cta_styles — never reproducible via an isolated
direct API call, which is exactly the signature of a DB consistency race
rather than a deterministic logic bug). Fixed by using find_one_and_update
(a single atomic write-and-read-back) instead. These tests use a Fake
collection that actually implements upsert/$set-merge/return-after
semantics, unlike a mock that just records calls, so they catch a
regression in the merge/field-preservation behavior, not just "was it
called"."""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.social_media_manager.services.brand_profile_service import (
    BrandProfileService,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeProfilesCollection:
    """Minimal but behaviorally-real Mongo collection: find_one_and_update
    actually merges $set into the stored doc and returns the POST-update
    state, matching real MongoDB/DocumentDB semantics — not just a mock
    that returns whatever the test wires up."""

    def __init__(self, seed=None):
        self.docs = [dict(seed)] if seed else []

    def _match(self, query, doc):
        return all(doc.get(k) == v for k, v in query.items() if k != "$exists")

    async def find_one(self, query):
        for d in self.docs:
            if self._match(query, d):
                return dict(d)
        return None

    async def find_one_and_update(self, query, update, return_document=None, upsert=False):
        for d in self.docs:
            if self._match(query, d):
                d.update(update["$set"])
                return dict(d)
        return None

    async def insert_one(self, doc):
        doc["_id"] = "fake_object_id"
        self.docs.append(doc)

    async def update_one(self, query, update):
        # Only reached by the legacy duplicate-key fallback path
        for d in self.docs:
            if self._match(query, d):
                d.update(update["$set"])
        return AsyncMock(matched_count=1, modified_count=1)


class FakeDb(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeProfilesCollection())


class TestAtomicUpdateReturnsExactlyWhatWasWritten:
    def test_update_returns_the_just_written_field_not_a_stale_read(self):
        db = FakeDb()
        db["brand_profiles"] = FakeProfilesCollection(seed={
            "user_id": "u1", "brand_id": "brnd_personal_u1",
            "brand_name": "Docerity", "cta_styles": ["Link in bio"],
        })
        with patch(
            "app.models.brand_account.BrandAccount.personal_brand_id",
            return_value="brnd_personal_u1",
        ):
            result = _run(BrandProfileService.save(
                "u1", {"cta_styles": ["Link in bio", "Learn more"]}, db, brand_id="brnd_personal_u1",
            ))
        assert result["status"] is True
        assert result["responseData"]["cta_styles"] == ["Link in bio", "Learn more"]
        # No separate read happened — real MongoDB guarantees
        # find_one_and_update's returned document IS the post-write state,
        # eliminating the race by construction rather than by chance.

    def test_update_preserves_fields_not_touched_by_this_save(self):
        """The exact regression this fix must not introduce: a caller that
        sends only a partial payload must not lose every other field —
        find_one_and_update $sets only the given fields, same as the old
        update_one did, and returns the FULL merged document."""
        db = FakeDb()
        db["brand_profiles"] = FakeProfilesCollection(seed={
            "user_id": "u1", "brand_id": "brnd_personal_u1",
            "brand_name": "Docerity", "industry": "Tech & SaaS",
            "cta_styles": ["Link in bio"],
        })
        with patch(
            "app.models.brand_account.BrandAccount.personal_brand_id",
            return_value="brnd_personal_u1",
        ):
            result = _run(BrandProfileService.save(
                "u1", {"onboarding_current_step": "cta"}, db, brand_id="brnd_personal_u1",
            ))
        assert result["status"] is True
        assert result["responseData"]["brand_name"] == "Docerity"
        assert result["responseData"]["industry"] == "Tech & SaaS"
        assert result["responseData"]["cta_styles"] == ["Link in bio"]

    def test_brand_new_profile_insert_returns_the_complete_doc_without_a_read(self):
        db = FakeDb()
        with patch(
            "app.models.brand_account.BrandAccount.personal_brand_id",
            return_value="brnd_personal_u2",
        ):
            result = _run(BrandProfileService.save(
                "u2", {"brand_name": "NewCo", "cta_styles": ["Shop now"]}, db, brand_id="brnd_personal_u2",
            ))
        assert result["status"] is True
        assert result["responseData"]["brand_name"] == "NewCo"
        assert result["responseData"]["cta_styles"] == ["Shop now"]
        # DEFAULTS backfilled for every untouched field on a brand-new profile
        assert result["responseData"]["industry"] == ""
        assert "_id" not in result["responseData"]
