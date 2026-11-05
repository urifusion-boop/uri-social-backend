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


def test_only_a_video_of_the_owner_or_team_gets_talking_points():
    assert cp.creator_brief("Shea Co", "", "", "", "staff_demo")["talking_points"] == cp.TALKING_POINTS
    for kind in ("real_customer", "paid_creator", "creator_style"):
        assert "talking_points" not in cp.creator_brief("Shea Co", "", "", "", kind)


def test_no_brief_endpoint_shot_carries_a_written_line(monkeypatch):
    from types import SimpleNamespace
    from app.agents.jane_ads import brief as ab_brief, creative, nl, router

    async def script(*_, **__):
        return SimpleNamespace(shots=[SimpleNamespace(seconds=5, direction="Hold the kit", say="It has lip balm")])

    async def parsed(*_):
        return SimpleNamespace(budget_ngn=None)

    async def brand(*_):
        return {"brand_name": "Shea Co"}

    async def no_brief(*_):
        return {}

    monkeypatch.setattr(creative, "write_shoot_script", script)
    monkeypatch.setattr(creative, "get_brand_context", brand)
    monkeypatch.setattr(nl, "parse_message", parsed)
    monkeypatch.setattr(ab_brief, "load", no_brief)
    out = run(router.creative_creator_brief({"ugc_kind": "staff_demo", "description": "kit"}, None,
                                            {"user_id": "u1", "brand_id": "b1"}))["brief"]
    assert out["shots"] == [{"shot": "Shot 1 · 5s", "direction": "Hold the kit"}]
    assert "lip balm" not in str(out)


def test_a_too_small_budget_is_explained_in_the_number_the_client_typed():
    from types import SimpleNamespace
    from app.agents.jane_ads import constants as C
    from app.agents.jane_ads.models import PlanAdvice
    from app.agents.jane_ads.router import _budget_advice
    advice = PlanAdvice(reason="x", suggested_min_ngn=5000, can_pool=True, trace=[])
    parsed = SimpleNamespace(model_dump=lambda: {})
    out = _budget_advice(parsed, advice, 5000, False)["early_return"]["advice"]
    assert "₦5,000 leaves ₦4,500" in out["reason"] and "at least ₦5,600" in out["reason"]
    assert out["suggested_min_ngn"] == 5600 and C.ad_spend_from_budget(5600) >= 5000
    assert _budget_advice(parsed, advice, 4000, True)["early_return"]["advice"]["reason"] == "x"


def test_a_website_typed_in_chat_is_found_and_social_links_are_not():
    from app.agents.jane_ads.destination import website_in_text
    assert website_in_text("Website is https://urisocial.com. Budget 6,000") == "https://urisocial.com"
    assert website_in_text("see www.shop.ng, thanks") == "www.shop.ng"
    assert website_in_text("wa.me/234 and https://instagram.com/x") == ""
    assert website_in_text("no link here") == ""


def test_awareness_on_meta_is_described_as_reach_wherever_the_tap_goes():
    from app.agents.jane_ads.destination import DestinationType, optimization_for
    from app.agents.jane_ads.models import CampaignObjective
    assert optimization_for(DestinationType.WEBSITE, "Meta (Instagram & Facebook)", CampaignObjective.AWARENESS)[0] == "Reach"
    assert optimization_for(DestinationType.WEBSITE, "TikTok", CampaignObjective.AWARENESS)[0] == "Website link clicks"
    assert optimization_for(DestinationType.WEBSITE)[0] == "Website link clicks"


def test_concepts_follow_the_budget_and_carry_a_different_angle():
    from app.agents.jane_ads import concepts
    assert concepts.max_concepts(6000, 2) == 1
    assert concepts.max_concepts(42000, 7) == 3
    assert concepts.max_concepts(28000, 7) == 2
    assert "offer itself" in concepts.brief_for("Shea kit", "offer")
    assert concepts.brief_for("Shea kit", "") == "Shea kit"


def test_the_plan_card_says_how_concepts_split_the_budget():
    from app.agents.jane_ads.brief import structure
    plan = {"platforms": [{"budget_ngn": 42000, "days": 7}], "concepts": [{}, {}], "ad_layout": "abo"}
    s = structure(plan, {"budget_ngn": 42000})
    assert s["ad_sets"] == 3 and s["ads_per_ad_set"] == 1 and s["per_ad_set_daily_ngn"] == 2000
    shared = structure({**plan, "ad_layout": "single_ad_set"}, {"budget_ngn": 42000})
    assert shared["ad_sets"] == 1 and shared["ads_per_ad_set"] == 3 and "per_ad_set_daily_ngn" not in shared


def test_the_duration_the_client_typed_is_understood_and_the_latest_wins():
    from app.agents.jane_ads import concepts
    assert concepts.stated_days("Budget 14,000 for 2 days, all of Lagos") == 2
    assert concepts.stated_days("6,000 for 2 days. Make it 9,000 for 3 days") == 3
    assert concepts.stated_days("run it for 2 weeks") == 14 and concepts.stated_days("for a week") == 7
    assert concepts.stated_days("delivery in 3 days") is None
    assert concepts.days_for_budget(2, 12600, 2000) == 2
    assert concepts.days_for_budget(7, 6000, 2000) == 3 and concepts.days_for_budget(30, 90000, 2000) == 7


def test_each_ad_set_needs_the_daily_floor_before_it_can_split():
    from app.agents.jane_ads import concepts
    assert concepts.can_split(12600, 2, 3) and not concepts.can_split(6000, 2, 3)
    assert not concepts.can_split(100000, 2, 1)
    assert concepts.next_angle(["problem", ""]) == "offer"


def test_every_ad_in_the_test_answers_to_the_brief_not_just_the_first():
    clean = cp.provenance("ugc", ugc_kind="real_customer", rights_confirmed=True)
    no_consent = cp.provenance("ugc", ugc_kind="paid_creator")
    plan = {**_plan(clean), "concepts": [{"source": "upload", "headline": "h", "primary_text": "t",
                                          "provenance": no_consent}]}
    assert "ugc_rights_missing" in {i["code"] for i in ab.check_plan(plan, {"budget_ngn": 14000}, {})["blocking"]}
    generated = {**_plan({}), "concepts": [{"source": "generate", "headline": "h", "primary_text": "t"}]}
    rules = {"constraints": ["own_photos_only"]}
    assert "constraint_own_photos" in {i["code"] for i in ab.check_plan(generated, {"budget_ngn": 14000}, rules)["blocking"]}
