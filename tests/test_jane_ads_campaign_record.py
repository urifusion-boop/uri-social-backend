"""
The campaign decision record (CI-SPEC-01 Part 1).

Results are recoverable — Meta holds spend and conversations for any campaign that
ever ran. Decisions are not: which plans Jane ranked, which the client rejected, and
whether her recommendation matched their choice exist only if written down at the
moment they happen. These tests pin the two fields that carry most of that value.
"""
import asyncio

from app.agents.jane_ads.campaign_record import (
    RECORDS,
    VARIANTS,
    _bucket_context,
    save_generated_variants,
    write_campaign_record,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    async def update_one(self, query, update, upsert=False):
        key = query.get("campaign_id") or query.get("variant_group_id")
        doc = self.docs.setdefault(key, {})
        doc.update(update.get("$set", {}))
        for k, v in (update.get("$setOnInsert") or {}).items():
            doc.setdefault(k, v)

    async def find_one(self, query, projection=None):
        key = query.get("campaign_id") or query.get("variant_group_id")
        return dict(self.docs[key]) if key in self.docs else None


class FakeDb:
    def __init__(self):
        self.collections: dict = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


VARIANTS_FIXTURE = [
    {"rank": 1, "recommended": True, "who_its_for": "people fitting out a new place",
     "trigger": "moving in", "geo_pockets": ["Lekki"]},
    {"rank": 2, "recommended": False, "who_its_for": "landlords furnishing a let",
     "trigger": "new tenant", "geo_pockets": ["Ikeja"]},
    {"rank": 3, "recommended": False, "who_its_for": "office managers",
     "trigger": "refit", "geo_pockets": ["Victoria Island"]},
]


def _plan_doc(**kw):
    base = dict(
        business_id="brnd_1",
        variant_group_id="vgrp_1",
        selected_plan_variant=VARIANTS_FIXTURE[1],   # client took rank 2, NOT the pick
        jane_platforms=["meta"],
        forced_to_meta=False,
        understood={"category": "Home goods", "city": "Lagos", "geo_mode": "watering_hole",
                    "stated_behaviour": "discover", "offer_type": "product"},
        req={"creative_source": "generate", "budget_ngn": 18000.0, "behaviour": "discover"},
        plan={"days": 5, "destination_type": "whatsapp",
              "geo": {"city": "Lagos", "pins": [{"name": "Ikeja"}, {"name": "Yaba"}]},
              "creative": {"headline": "h", "image_url": "u", "is_video": False},
              "corpus_coverage": "partial",
              "corpus_citations": [{"strategy_id": "SEED-012", "version": 3}]},
    )
    base.update(kw)
    return base


def _write(db, doc=None):
    _run(save_generated_variants(
        db, variant_group_id="vgrp_1", brand_id="brnd_1", business_id="brnd_1",
        variants=VARIANTS_FIXTURE, recommended_rank=1,
    ))
    _run(write_campaign_record(
        db, campaign_id="c1", brand_id="brnd_1", business_id="brnd_1",
        plan_doc=doc or _plan_doc(), stated_budget_ngn=20_000.0,
        ad_spend_ngn=18_000.0, service_fee_ngn=2_000.0,
    ))
    return db[RECORDS].docs["c1"]


# ── The two fields that exist nowhere else ───────────────────────────────────

def test_rejected_plans_are_kept_not_just_the_winner():
    """A plan Jane ranks first that clients keep declining means the ranking is
    wrong — and storing only the winner throws away the comparison that shows it."""
    rec = _write(FakeDb())
    assert len(rec["strategy"]["plans_generated"]) == 3
    ranks = sorted(v["rank"] for v in rec["strategy"]["plans_generated"])
    assert ranks == [1, 2, 3]


def test_recommended_and_selected_are_stored_separately():
    rec = _write(FakeDb())
    assert rec["strategy"]["plan_recommended"]["rank"] == 1
    assert rec["strategy"]["plan_selected"]["rank"] == 2


def test_divergence_is_flagged_when_the_client_overrides_the_recommendation():
    """The most direct measure of whether Jane's strategic reasoning matches what
    clients actually want."""
    rec = _write(FakeDb())
    assert rec["strategy"]["recommendation_diverged"] is True


def test_no_divergence_when_the_client_takes_the_recommendation():
    doc = _plan_doc(selected_plan_variant=VARIANTS_FIXTURE[0])
    rec = _write(FakeDb(), doc)
    assert rec["strategy"]["recommendation_diverged"] is False


# ── Bucket keys (§2.2) — cannot be formed retroactively ──────────────────────

def test_primary_bucket_keys_are_tagged_on_every_record():
    rec = _write(FakeDb())
    ctx = rec["context"]
    assert ctx["business_category"] == "home goods"
    assert ctx["city"] == "lagos"
    assert ctx["budget_tier"]      # from the SAME thresholds the A/B scope uses
    assert ctx["platform"] == "meta"


def test_secondary_keys_are_captured_even_though_nothing_slices_on_them_yet():
    rec = _write(FakeDb())
    ctx = rec["context"]
    assert ctx["area"] == ["Ikeja", "Yaba"]
    assert ctx["conversion_location"] == "whatsapp"
    assert ctx["purchase_behaviour"] == "discover"


def test_corpus_citations_keep_their_version():
    rec = _write(FakeDb())
    cited = rec["strategy"]["corpus_records_cited"]
    assert cited and cited[0]["version"] == 3


def test_budget_split_records_what_was_actually_charged():
    rec = _write(FakeDb())
    assert rec["budget"]["stated_ngn"] == 20_000.0
    assert rec["budget"]["effective_spend_ngn"] == 18_000.0
    assert rec["budget"]["service_fee_ngn"] == 2_000.0


# ── It must never cost a launch ──────────────────────────────────────────────

def test_a_broken_plan_doc_never_raises():
    """The client has already been charged by the time this runs. A lost record costs
    one data point; a raised exception costs the campaign."""
    db = FakeDb()
    _run(write_campaign_record(
        db, campaign_id="c2", brand_id="b", business_id="b",
        plan_doc={"plan": "not-a-dict"}, stated_budget_ngn=0, ad_spend_ngn=0,
        service_fee_ngn=0,
    ))  # must not raise


def test_a_missing_db_is_a_no_op():
    _run(save_generated_variants(None, variant_group_id="v", brand_id="b",
                                 business_id="b", variants=[]))
    _run(write_campaign_record(None, campaign_id="c", brand_id="b", business_id="b",
                               plan_doc={}, stated_budget_ngn=0, ad_spend_ngn=0,
                               service_fee_ngn=0))


def test_the_record_is_immutable_once_written():
    """Written once at launch. A second write must not rewrite the decisions — the
    only part meant to change later is results, backfilled from Meta."""
    db = FakeDb()
    _write(db)
    doc = _plan_doc(selected_plan_variant=VARIANTS_FIXTURE[0])
    _run(write_campaign_record(
        db, campaign_id="c1", brand_id="brnd_1", business_id="brnd_1",
        plan_doc=doc, stated_budget_ngn=99_999.0, ad_spend_ngn=1.0, service_fee_ngn=1.0,
    ))
    rec = db[RECORDS].docs["c1"]
    assert rec["budget"]["stated_ngn"] == 20_000.0
    assert rec["strategy"]["plan_selected"]["rank"] == 2
