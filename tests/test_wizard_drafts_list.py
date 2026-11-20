"""GET /jane-ads/wizard-drafts skips a draft saved before drafts had ids — reading one crashed the
whole list with a 500 (live on staging: KeyError 'draft_id'), so drafts from another device never loaded."""
import asyncio
from datetime import datetime, timezone

from app.agents.jane_ads import router as R
from app.agents.jane_ads import wizard_draft


def test_a_draft_without_an_id_is_skipped_and_the_rest_still_list(monkeypatch):
    now = datetime.now(timezone.utc)

    async def fake_list(db, brand):
        return [{"brand_id": brand, "payload": {"old": True}, "updated_at": now},
                {"brand_id": brand, "draft_id": "abc", "payload": {"objective": {}}, "updated_at": now}]

    monkeypatch.setattr(wizard_draft, "list_drafts", fake_list)
    out = asyncio.get_event_loop().run_until_complete(
        R.list_wizard_drafts(brand_ctx={"brand_id": "b1", "user_id": "u1"}, db=None))
    assert [d["draft_id"] for d in out["drafts"]] == ["abc"]
