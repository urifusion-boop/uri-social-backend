"""save_image_version/undo_image_edit's handling of background_image_url and
logo_placement — added so a logo reposition (see LogoRepositionService)
plugs into the existing version-history/undo system correctly: undoing past
a reposition should restore Move-Logo-eligibility to whatever it was at that
earlier version, not leave it pointed at a background/placement that no
longer matches the restored image."""
import asyncio
from unittest.mock import MagicMock

from app.agents.social_media_manager.services.image_editing_service import (
    ImageEditingService,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeVersionsCollection:
    """Enough of a Mongo collection to exercise the real query/update logic
    in save_image_version and undo_image_edit — find_one/find both respect
    the filter, unlike the simpler FakeCollection used elsewhere that always
    returns one fixed doc regardless of query."""

    def __init__(self):
        self.docs = []

    async def update_many(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update["$set"])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    def find(self, query):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return FakeCursor(matches)

    async def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update["$set"])


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, field, direction):
        self._docs = sorted(self._docs, key=lambda d: d[field], reverse=(direction < 0))
        return self

    async def to_list(self, length=None):
        return self._docs[:length] if length else self._docs


class FakeDraftsCollection:
    def __init__(self, doc):
        self.doc = doc
        self.updates = []

    async def find_one(self, query):
        return self.doc

    async def update_one(self, query, update):
        self.updates.append((query, update))
        self.doc.update(update["$set"])
        return MagicMock(matched_count=1)


class FakeDb(dict):
    def __getitem__(self, name):
        return self.setdefault(name, FakeVersionsCollection())


class TestSaveImageVersionCarriesLogoFields:
    def test_stores_background_and_placement_when_given(self):
        db = FakeDb()
        _run(ImageEditingService.save_image_version(
            db=db, draft_id="d1", version_number=2, image_url="https://x/v2.png",
            edit_category="logo_reposition", edit_feedback="Moved logo",
            background_image_url="https://x/bg.png",
            logo_placement={"x": 1, "y": 2, "width": 3, "height": 4},
        ))
        saved = db["image_versions"].docs[0]
        assert saved["background_image_url"] == "https://x/bg.png"
        assert saved["logo_placement"] == {"x": 1, "y": 2, "width": 3, "height": 4}

    def test_defaults_to_none_when_not_given(self):
        db = FakeDb()
        _run(ImageEditingService.save_image_version(
            db=db, draft_id="d1", version_number=2, image_url="https://x/v2.png",
            edit_category="style_edit", edit_feedback="Change colours",
        ))
        saved = db["image_versions"].docs[0]
        assert saved["background_image_url"] is None
        assert saved["logo_placement"] is None


class TestUndoRestoresLogoFields:
    def _seed(self, db, versions):
        vc = db["image_versions"]
        for v in versions:
            vc.docs.append(v)

    def test_undo_past_a_reposition_restores_prior_placement(self):
        # v1: original generation (has a background/placement). v2: a logo
        # reposition (different placement, same background). Draft is
        # currently on v2; undo should land back on v1's own values.
        db = FakeDb()
        db["content_drafts"] = FakeDraftsCollection({
            "id": "d1", "draft_id": "d1", "user_id": "u1",
            "image_url": "https://x/v2.png", "image_version": 2,
            "background_image_url": "https://x/bg.png",
            "logo_placement": {"x": 50, "y": 50, "width": 20, "height": 20},
        })
        self._seed(db, [
            {
                "id": "v1", "draft_id": "d1", "slide_index": None, "version_number": 1,
                "image_url": "https://x/v1.png", "background_image_url": "https://x/bg.png",
                "logo_placement": {"x": 0, "y": 0, "width": 10, "height": 10},
                "is_current": False,
            },
            {
                "id": "v2", "draft_id": "d1", "slide_index": None, "version_number": 2,
                "image_url": "https://x/v2.png", "background_image_url": "https://x/bg.png",
                "logo_placement": {"x": 50, "y": 50, "width": 20, "height": 20},
                "is_current": True,
            },
        ])

        result = _run(ImageEditingService.undo_image_edit(db, "d1", "u1"))

        assert result["status"] is True
        drafts = db["content_drafts"]
        update = drafts.updates[0][1]["$set"]
        assert update["image_url"] == "https://x/v1.png"
        assert update["logo_placement"] == {"x": 0, "y": 0, "width": 10, "height": 10}
        assert update["background_image_url"] == "https://x/bg.png"

    def test_undo_to_a_version_predating_the_logo_feature_clears_fields(self):
        # v1 was saved before background/logo_placement existed on versions
        # at all (an older draft) — undoing to it must clear the CURRENT
        # (stale) background/placement rather than leaving them in place.
        db = FakeDb()
        db["content_drafts"] = FakeDraftsCollection({
            "id": "d1", "draft_id": "d1", "user_id": "u1",
            "image_url": "https://x/v2.png", "image_version": 2,
            "background_image_url": "https://x/bg.png",
            "logo_placement": {"x": 50, "y": 50, "width": 20, "height": 20},
        })
        self._seed(db, [
            {
                "id": "v1", "draft_id": "d1", "slide_index": None, "version_number": 1,
                "image_url": "https://x/v1.png", "is_current": False,
                # no background_image_url/logo_placement keys at all
            },
            {
                "id": "v2", "draft_id": "d1", "slide_index": None, "version_number": 2,
                "image_url": "https://x/v2.png", "background_image_url": "https://x/bg.png",
                "logo_placement": {"x": 50, "y": 50, "width": 20, "height": 20},
                "is_current": True,
            },
        ])

        result = _run(ImageEditingService.undo_image_edit(db, "d1", "u1"))

        assert result["status"] is True
        update = db["content_drafts"].updates[0][1]["$set"]
        assert update["image_url"] == "https://x/v1.png"
        assert update["background_image_url"] is None
        assert update["logo_placement"] is None
