"""
Fixes from testing "Make several ads" on prod, 2026-11-13:

- Ask only answered questions: "Change the second ad headline to …" was told it "sounds like a new
  campaign". Ask now edits an ad's words, through the same endpoints the Edit words button uses.
- A News Headline ad painted "New branch in Lekki opens" and "7 days" for a brief that announced
  nothing — the 7 days was the campaign length left in the brief.
- A swapped banned word started a headline in lower case ("simple process Await").
- A brand's first-ever destination read back empty from a lagging replica, so "Use this" answered
  "Your ads don't have a destination yet".
- A brief describing a different product than the brand profile planned for the profile.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.agents.jane_ads import router as r
from app.agents.jane_ads.models import AdCreative
from app.agents.jane_ads.plan_defence import FollowupIntent, classify_followup
from app.agents.social_media_manager.services import brand_brief
from tests.test_jane_ads_plan_defence import _mock_openai_response, _plan, _req


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Ask can change an ad's words ────────────────────────────────────────────────

def _ad(h, t):
    return AdCreative(image_url="https://x/img.png", headline=h, primary_text=t)


class _Plans:
    def __init__(self, doc):
        self.doc = doc
        self.pushed = []

    async def find_one(self, *a, **k):
        return self.doc

    async def update_one(self, filt, upd, *a, **k):
        self.pushed.append(upd)


def _doc(n_extra=1, status="pending"):
    plan = _plan(creative=_ad("Your Stress-Free Ride", "Let us take the wheel."),
                 concepts=[_ad("simple process Await", "Navigating your commute just got easier.")] * n_extra)
    return {"plan_id": "p1", "brand_id": "br1", "status": status, "message": "brief",
            "plan": plan.model_dump(mode="json"), "req": _req().model_dump(mode="json")}


def _ask(doc, intent, question="x"):
    db = {"jane_ads_pending_plans": _Plans(doc)}
    edit_ad = AsyncMock(return_value={"concepts": [{"headline": "edited"}]})
    edit_fields = AsyncMock(return_value={"rejected": [], "creative": {"headline": "edited main"}, "fields": []})
    with patch.object(r, "meta_plan_edit_ad", new=edit_ad), \
         patch.object(r, "meta_plan_edit_fields", new=edit_fields), \
         patch("app.agents.jane_ads.plan_defence.classify_followup", new=AsyncMock(return_value=intent)):
        out = _run(r.meta_plan_ask("p1", r.PlanAskBody(question=question), db, {"brand_id": "br1"}))
    return out, edit_ad, edit_fields


def test_ask_changes_the_second_ads_headline_to_the_exact_words_given():
    intent = FollowupIntent(kind="edit_ad", ad_number=2, ad_field="headline", new_text="Get to work stress-free")
    out, edit_ad, edit_fields = _ask(_doc(), intent)
    assert out["kind"] == "edit_ad" and out["ad_number"] == 2
    index, body = edit_ad.await_args.args[1], edit_ad.await_args.args[2]
    assert index == 0
    assert body.headline == "Get to work stress-free"
    assert body.primary_text == "Navigating your commute just got easier."   # caption untouched
    assert "Get to work stress-free" in out["answer"]
    assert out["concepts"] == [{"headline": "edited"}]
    assert edit_fields.await_count == 0


def test_ask_edits_the_first_ad_through_the_review_fields():
    intent = FollowupIntent(kind="edit_ad", ad_number=1, ad_field="caption", new_text="Book your seat today.")
    out, edit_ad, edit_fields = _ask(_doc(), intent)
    edits = edit_fields.await_args.args[1].edits
    assert edits == {"headline": "Your Stress-Free Ride", "caption": "Book your seat today."}
    assert out["creative"] == {"headline": "edited main"} and edit_ad.await_count == 0


def test_ask_rewrites_when_the_client_describes_the_change():
    intent = FollowupIntent(kind="edit_ad", ad_number=2, ad_field="headline", instruction="make it clearer")
    with patch.object(r, "meta_plan_edit_ad", new=AsyncMock(return_value={"concepts": []})) as edit_ad, \
         patch("app.agents.jane_ads.plan_defence.classify_followup", new=AsyncMock(return_value=intent)), \
         patch("app.agents.jane_ads.plan_defence.rewrite_ad_words",
               new=AsyncMock(return_value=("A calmer commute", "Navigating your commute just got easier."))):
        out = _run(r.meta_plan_ask("p1", r.PlanAskBody(question="make ad 2 clearer"),
                                   {"jane_ads_pending_plans": _Plans(_doc())}, {"brand_id": "br1"}))
    assert edit_ad.await_args.args[2].headline == "A calmer commute"
    assert "A calmer commute" in out["answer"]


def test_ask_asks_which_ad_when_there_are_several_and_none_was_named():
    out, edit_ad, edit_fields = _ask(_doc(n_extra=2), FollowupIntent(kind="edit_ad", new_text="Hi"))
    assert out["answer"].startswith("Which ad should I change — 1, 2 or 3?")
    assert edit_ad.await_count == 0 and edit_fields.await_count == 0


def test_ask_with_one_ad_needs_no_number():
    out, _, edit_fields = _ask(_doc(n_extra=0), FollowupIntent(kind="edit_ad", ad_field="headline", new_text="Ride easy"))
    assert edit_fields.await_args.args[1].edits["headline"] == "Ride easy"


def test_ask_names_the_real_count_for_an_ad_that_isnt_there():
    out, edit_ad, _ = _ask(_doc(), FollowupIntent(kind="edit_ad", ad_number=5, new_text="x", ad_field="headline"))
    assert out["answer"] == "This plan has 2 ads — which one did you mean?"
    assert edit_ad.await_count == 0


def test_a_refused_edit_says_why_instead_of_failing():
    intent = FollowupIntent(kind="edit_ad", ad_number=2, ad_field="headline", new_text="x" * 60)
    db = {"jane_ads_pending_plans": _Plans(_doc())}
    with patch.object(r, "meta_plan_edit_ad",
                      new=AsyncMock(side_effect=HTTPException(400, "Keep the headline to 40 characters."))), \
         patch("app.agents.jane_ads.plan_defence.classify_followup", new=AsyncMock(return_value=intent)):
        out = _run(r.meta_plan_ask("p1", r.PlanAskBody(question="q"), db, {"brand_id": "br1"}))
    assert out["answer"] == "I couldn't make that change: Keep the headline to 40 characters."


def test_a_launched_campaigns_ads_are_not_edited_here():
    out, edit_ad, edit_fields = _ask(_doc(status="launched"),
                                     FollowupIntent(kind="edit_ad", ad_number=2, ad_field="headline", new_text="x"))
    assert "already gone out" in out["answer"]
    assert edit_ad.await_count == 0 and edit_fields.await_count == 0


def test_classifier_reads_an_ad_edit():
    reply = {"kind": "edit_ad", "ad_number": 2, "ad_field": "headline",
             "new_text": "“Get to work stress-free”", "instruction": "change the headline"}
    with patch("openai.AsyncOpenAI") as cls:
        cls.return_value.chat.completions.create = AsyncMock(return_value=_mock_openai_response(reply))
        intent = _run(classify_followup("Change the second ad headline to: Get to work stress-free"))
    assert intent.kind == "edit_ad" and intent.ad_number == 2 and intent.ad_field == "headline"
    assert intent.new_text == "Get to work stress-free"


def test_classifier_ignores_a_nonsense_ad_number():
    reply = {"kind": "edit_ad", "ad_number": 0, "ad_field": "title", "new_text": "", "instruction": "shorter"}
    with patch("openai.AsyncOpenAI") as cls:
        cls.return_value.chat.completions.create = AsyncMock(return_value=_mock_openai_response(reply))
        intent = _run(classify_followup("make it shorter"))
    assert intent.ad_number is None and intent.ad_field == "both"


# ── News Headline only states what the client actually wrote ────────────────────

UNANNOUNCED = ("Uri Social — an app that creates and posts social media content and ads for small "
               "businesses. For small business owners who are too busy to manage their own social media. "
               "in Lekki, Lagos.")


def test_an_invented_announcement_is_refused():
    from app.agents.jane_ads.vsg01_orchestrator import _news_grounded
    assert not _news_grounded("New branch in Lekki opens", UNANNOUNCED)
    assert not _news_grounded("Grand opening sale", "We're opening a new branch in Yaba on 1 October")


def test_a_real_announcement_still_qualifies():
    from app.agents.jane_ads.vsg01_orchestrator import _news_grounded
    assert _news_grounded("New Yaba branch now open", "We're opening a new branch in Yaba on 1 October")
    assert _news_grounded("Term 2 admissions open", "Term 2 admissions are now open, closing 30 September")


def test_news_headline_is_skipped_when_the_model_invents_one():
    from app.agents.jane_ads import vsg01_orchestrator as v
    invented = {"headline": "New branch in Lekki opens", "secondary_line": "Digital marketing support now",
                "date_stamp": "7 days", "announcement_subject": "a ribbon cutting", "nigerian_setting": "x"}
    with patch.object(v, "_call_content_model", new=AsyncMock(return_value=invented)):
        assert _run(v._content_news_headline("Uri Social", "software", UNANNOUNCED)) is None


def test_a_date_the_client_never_gave_is_dropped():
    from app.agents.jane_ads import vsg01_orchestrator as v
    src = "We're opening a new branch in Yaba, tell people about it"
    got = {"headline": "New Yaba branch now open", "secondary_line": "", "date_stamp": "7 days",
           "announcement_subject": "doors opening", "nigerian_setting": v._NIGERIAN_SETTINGS[0]}
    with patch.object(v, "_call_content_model", new=AsyncMock(return_value=got)):
        out = _run(v._content_news_headline("Mama Kitchen", "restaurant", src))
    assert out["headline"] == "New Yaba branch now open" and out["date_stamp"] is None


def test_the_campaign_length_never_reaches_the_ad_writer():
    from app.agents.jane_ads.creative_paths import without_budget
    brief = UNANNOUNCED + " Budget: ₦30000 over 7 days."
    assert without_budget(brief, 30000) == UNANNOUNCED
    assert without_budget(brief, None) == UNANNOUNCED
    assert without_budget("Fresh bread in Yaba, 30,000 naira over 7 days", 30000) == "Fresh bread in Yaba"


# ── A swapped word keeps its capitals ───────────────────────────────────────────

def test_a_swapped_first_word_keeps_its_capital():
    assert brand_brief.enforce("Effortless rides await you", {}, append_cta=False) == "Simple rides await you"
    assert brand_brief.enforce("SEAMLESS RIDES", {}, append_cta=False) == "SMOOTH RIDES"
    assert brand_brief.enforce("We make it effortless.", {}, append_cta=False) == "We make it simple."


# ── The destination just typed is the one used ──────────────────────────────────

def test_a_first_destination_is_read_from_the_write_not_a_lagging_replica():
    from app.agents.jane_ads.destination import DestinationType, set_brand_destination

    class LaggingReplica:
        async def find_one_and_update(self, filt, upd, upsert=False, return_document=None):
            return {"brand_id": filt["brand_id"], **upd["$set"]}

        async def find_one(self, *a, **k):
            return None   # what a replica that hasn't caught up returns

    from app.agents.jane_ads import whatsapp
    db = {whatsapp.SETTINGS_COLLECTION: LaggingReplica()}
    out = _run(set_brand_destination(db, "br1", DestinationType.WEBSITE, website_url="urisocial.com",
                                     destination_cta="learn_more"))
    assert out["destination_type"] == "website" and out["chosen"] is True
    assert out["website_url"].startswith("https://urisocial.com")


# ── What the client is promoting now beats the saved profile ────────────────────

def test_the_consultant_is_told_the_clients_words_beat_the_profile():
    from app.agents.jane_ads import jane_consultant as jc
    seen = {}

    async def create(**kw):
        seen["prompt"] = json.dumps(kw["messages"])
        return _mock_openai_response({"business_name": "Uri Social", "category": "software"})

    with patch("openai.AsyncOpenAI") as cls, patch.object(jc.settings, "OPENAI_API_KEY", "k", create=True):
        cls.return_value.chat.completions.create = AsyncMock(side_effect=create)
        try:
            brief = _run(jc.consult("Uri Social, an app that posts content for small businesses", "Magen",
                                    "Transportation"))
        except Exception:
            brief = None
    assert "their words win" in seen.get("prompt", "")
    if brief is not None:
        assert brief.business_name == "Uri Social"
