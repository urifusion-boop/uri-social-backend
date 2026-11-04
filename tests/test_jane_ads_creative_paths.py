"""Creative paths (AI video, UGC, creator briefs), Guided/Advanced mode, and Text on a
Face never putting invented words in a customer's mouth."""
import pytest
from fastapi import HTTPException

from app.agents.jane_ads import brief as ab
from app.agents.jane_ads import creative_paths as cp
from app.agents.jane_ads.ad_formats import text_on_a_face as toaf
from app.agents.jane_ads.vsg01_orchestrator import _vsg01_candidate_params

from tests.test_jane_ads_growth import mongomock_motor, run  # noqa: F401

PHOTO = "https://example.com/face.jpg"


# ── Text on a Face ────────────────────────────────────────────────────────────

def test_a_composed_line_cannot_go_across_a_customers_face():
    with pytest.raises(toaf.WordsInSomeoneElsesMouth):
        toaf.build_document(PHOTO, "No job too hard.", True, speaker="customer")
    with pytest.raises(toaf.WordsInSomeoneElsesMouth):
        toaf.build_document(PHOTO, "No job too hard.", True, speaker="customer",
                            customer_words="They were quick and friendly.")


def test_a_customers_own_words_or_the_owners_voice_pass_the_speaker_check():
    toaf._check_speaker("Quick and friendly!", "customer", "Honestly they were quick and friendly! 5 stars")
    toaf._check_speaker("No job too hard.", "owner", "")
    with pytest.raises(toaf.WordsInSomeoneElsesMouth):
        toaf._check_speaker("No job too hard.", "stranger", "")


def test_text_on_a_face_is_offered_for_an_owner_photo_not_a_customer_photo():
    customer = _vsg01_candidate_params(photo_url=PHOTO, photo_attestation="real_customer_photo", recomposite=False)
    owner = _vsg01_candidate_params(photo_url=PHOTO, photo_attestation="owner_photo", recomposite=False)
    assert "SEED-082" not in customer[0] and "SEED-074" in customer[0] and customer[2]
    assert "SEED-082" in owner[0] and "SEED-074" not in owner[0] and owner[2] and not owner[1]


def test_an_unknown_attestation_gets_no_photo_formats():
    ids, has_product, has_person, url = _vsg01_candidate_params(
        photo_url=PHOTO, photo_attestation="somebody", recomposite=False)
    assert url is None and not has_product and not has_person


# ── Paths and the brief ───────────────────────────────────────────────────────

def test_constraints_close_paths_and_recommendation_never_forces_video():
    open_paths = cp.availability({"constraints": ["no_ai_video", "no_ugc"]})
    assert not open_paths["ai_video"]["open"] and not open_paths["ugc"]["open"] and open_paths["static"]["open"]
    rec = cp.recommend("restaurant", "jollof in Surulere", {"constraints": ["no_ai_video"]})
    assert rec["primary"] == "static" and rec["ai_video_note"] is None
    assert cp.recommend("restaurant", "", {}, has_own_media=True)["primary"] == "upload"
    assert cp.recommend("", "", {"constraints": ["own_photos_only"]})["primary"] == "upload"


def test_ugc_is_suggested_only_where_trust_or_demonstration_sells():
    assert cp.recommend("skincare", "serums", {})["ugc"]["suggested"]
    assert not cp.recommend("cement supplier", "bulk bags, fixed price", {})["ugc"]["suggested"]
    assert cp.recommend("skincare", "", {"constraints": ["no_ugc"]})["ugc"] is None


def _plan(prov, primary_text="Fresh jollof, delivered hot."):
    return {"geo": {"mode": "own_radius", "city": "Lagos", "pins": [{"name": "Surulere"}]},
            "objective": "engagement", "destination_type": "whatsapp",
            "creative": {"source": "upload", "headline": "Order now", "primary_text": primary_text,
                         "provenance": prov},
            "platforms": [{"budget_ngn": 14000, "days": 7}]}


def codes(plan, brief):
    return {i["code"] for i in ab.check_plan(plan, {"budget_ngn": 14000}, brief)["blocking"]}


def test_ai_video_respects_no_ai_video_and_no_generated_people():
    video = cp.provenance("ai_video", video_job_id="j1", has_people=True)
    assert "constraint_no_ai_video" in codes(_plan(video), {"constraints": ["no_ai_video"]})
    assert "constraint_no_generated_people" in codes(_plan(video), {"constraints": ["no_generated_people"]})
    product_only = cp.provenance("ai_video", video_job_id="j1", has_people=False)
    assert not codes(_plan(product_only), {"constraints": ["no_generated_people"]})


def test_ugc_needs_consent_and_is_blocked_by_no_ugc():
    no_rights = cp.provenance("ugc", ugc_kind="real_customer")
    assert "ugc_rights_missing" in codes(_plan(no_rights), {})
    ok = cp.provenance("ugc", ugc_kind="real_customer", rights_confirmed=True)
    assert not codes(_plan(ok), {})
    assert "constraint_no_ugc" in codes(_plan(ok), {"constraints": ["no_ugc"]})


def test_only_a_real_customer_can_be_called_a_customer():
    claim = "Hear from our customer: best jollof in Lagos!"
    real = cp.provenance("ugc", ugc_kind="real_customer", rights_confirmed=True)
    assert not codes(_plan(real, claim), {})
    for kind in ("paid_creator", "creator_style", "staff_demo"):
        fake = cp.provenance("ugc", ugc_kind=kind, rights_confirmed=True)
        assert "misleading_customer_claim" in codes(_plan(fake, claim), {}), kind
    synthetic = cp.provenance("ai_video", video_job_id="j", has_people=True)
    assert "misleading_customer_claim" in codes(_plan(synthetic, "A happy customer says it's great"), {})
    assert not cp.claims_customer("Customers in Surulere order before noon")


def test_a_paid_creator_is_disclosed_in_the_ad_once():
    prov = cp.provenance("ugc", ugc_kind="paid_creator", rights_confirmed=True)
    text = cp.disclose("Try our new serum.", prov)
    assert text.endswith("Paid partnership.") and cp.disclose(text, prov) == text
    assert cp.disclose("Try it.", cp.provenance("ugc", ugc_kind="real_customer")) == "Try it."


def test_provenance_rejects_unknown_paths_and_kinds():
    with pytest.raises(ValueError):
        cp.provenance("hologram")
    with pytest.raises(ValueError):
        cp.provenance("ugc", ugc_kind="influencer")


def test_creator_brief_names_message_proof_shots_disclosure_and_rights():
    b = cp.creator_brief("Mama Put", "restaurant", "jollof", "₦2,500 combo", "paid_creator")
    assert {"message", "proof", "shots", "disclosure", "usage_rights", "honesty"} <= set(b)
    assert "Paid partnership" in b["disclosure"] and "₦2,500 combo" in b["message"]


def test_people_in_a_generated_video_are_detected():
    assert cp.video_has_people({"clips": [{"routed_model": "fal-ai/ai-avatar/single-text"}]})
    assert cp.video_has_people({"clips": [{"video_prompt": "A smiling woman tastes the jollof"}]})
    assert not cp.video_has_people({"clips": [{"video_prompt": "Steam rises from a plate of jollof rice"}]})


def test_ai_videos_are_listed_and_resolved_only_for_their_owner():
    db = mongomock_motor.AsyncMongoMockClient()["paths"]
    run(db[cp.VIDEO_JOBS].insert_many([
        {"job_id": "mine", "user_id": "u1", "status": "complete",
         "clips": [{"video_url": "https://v/1.mp4", "duration_seconds": 5, "video_prompt": "jollof steam"}]},
        {"job_id": "running", "user_id": "u1", "status": "generating", "clips": []},
        {"job_id": "theirs", "user_id": "u2", "status": "complete", "clips": [{"video_url": "https://v/2.mp4"}]},
    ]))
    listed = run(cp.ai_videos(db, "u1"))
    assert [v["video_job_id"] for v in listed] == ["mine"] and not listed[0]["has_people"]
    url, _ = run(cp.resolve_ai_video(db, "u1", "mine"))
    assert url == "https://v/1.mp4"
    assert run(db[cp.VIDEO_JOBS].find_one({"job_id": "mine"}))["merged_video_url"] == url
    for job in ("theirs", "running"):
        with pytest.raises(LookupError):
            run(cp.resolve_ai_video(db, "u1", job))


def test_resolving_an_ugc_pick_needs_a_video_and_a_kind():
    from app.agents.jane_ads.router import MetaLaunchFromMessageBody, _resolve_creative_path
    db = mongomock_motor.AsyncMongoMockClient()["paths"]
    body = MetaLaunchFromMessageBody(message="x", creative_path="ugc", ugc_kind="staff_demo")
    with pytest.raises(HTTPException):
        run(_resolve_creative_path(body, {"user_id": "u1"}, db))
    body = MetaLaunchFromMessageBody(message="x", creative_path="ugc", ugc_kind="staff_demo",
                                     reference_image_url="https://v/me.mp4", is_video=True,
                                     ugc_rights_confirmed=True)
    prov = run(_resolve_creative_path(body, {"user_id": "u1"}, db))
    assert body.creative_source == "upload" and prov["ugc_kind"] == "staff_demo" and not prov["synthetic"]
    assert run(_resolve_creative_path(MetaLaunchFromMessageBody(message="x"), {}, db)) == {}


# ── Guided / Advanced ─────────────────────────────────────────────────────────

def test_mode_defaults_to_guided_is_per_user_and_validated():
    db = mongomock_motor.AsyncMongoMockClient()["modes"]
    assert run(ab.load_mode(db, "b1", "u1")) == "guided"
    run(ab.save_mode(db, "b1", "u1", "advanced"))
    assert run(ab.load_mode(db, "b1", "u1")) == "advanced" and run(ab.load_mode(db, "b1", "u2")) == "guided"
    with pytest.raises(ValueError):
        run(ab.save_mode(db, "b1", "u1", "expert"))


def test_every_kind_of_video_tells_the_script_writer_who_is_filming():
    assert set(cp.FILMED_BY) == set(cp.UGC_KINDS)
    assert "not the owner" in cp.FILMED_BY["creator_style"] and "owner" in cp.FILMED_BY["staff_demo"]


def test_the_shoot_script_prompt_names_the_filmer_and_the_promoted_product(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.agents.jane_ads import creative
    sent = {}

    class Completions:
        async def create(self, **kw):
            sent["prompt"] = kw["messages"][0]["content"]
            msg = SimpleNamespace(content=json.dumps({"shots": [{"direction": "x", "say": "", "seconds": 5}]}))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(creative, "settings", SimpleNamespace(jane_ads_openai_key="k"))
    monkeypatch.setattr(creative.openai, "AsyncOpenAI",
                        lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    run(creative.write_shoot_script("Uri Social", "software", "messages", "trust",
                                    "shea butter starter kit", {}, filmed_by=cp.FILMED_BY["paid_creator"]))
    assert cp.FILMED_BY["paid_creator"] in sent["prompt"] and "shea butter starter kit" in sent["prompt"]
    assert "The business owner will film" not in sent["prompt"]
    assert "never put them in the script" in sent["prompt"]


def test_the_brief_message_never_pastes_the_chat():
    chat = "Promote our kit, ₦20,000 for 7 days. Static images are fine"
    assert "₦20,000" not in cp.creator_brief("Shea Co", "", chat)["message"]


def test_the_ad_budget_is_taken_out_before_a_script_is_written():
    chat = "Promote our ₦8,000 starter kit, ₦20,000 for 7 days. All of Lekki"
    assert cp.without_budget(chat, 20000) == "Promote our ₦8,000 starter kit. All of Lekki"
    assert "20k" not in cp.without_budget("Kit is 8k, budget 20k", 20000)
    assert cp.without_budget("Bags at ₦120,000", 20000) == "Bags at ₦120,000"
    assert cp.without_budget(chat, None) == chat
