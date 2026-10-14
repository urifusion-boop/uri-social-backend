"""
Display, YouTube & Discover and Everywhere (Performance Max): images cropped to
Google's shapes, a logo always present, copy within Google's limits, and one
atomic request whose pieces reference each other correctly.
"""
import base64
import io

import pytest
from PIL import Image

from app.agents.jane_ads.adapters import google_visual as gv
from app.agents.jane_ads.adapters.google import rsa_assets


def _png(w, h, color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _size(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64))).size


class TestImagesFitGooglesShapes:
    @pytest.mark.parametrize("w, h", [(1024, 1024), (1536, 1024), (1024, 1536)])
    def test_any_source_becomes_landscape_square_and_portrait(self, w, h):
        out = gv.marketing_images(_png(w, h))
        assert _size(out["landscape"]) == (1200, 628)
        assert _size(out["square"]) == (1200, 1200)
        assert _size(out["portrait"]) == (960, 1200)

    def test_the_brands_logo_is_placed_on_a_square(self):
        assert _size(gv.logo_image(_png(800, 200), "Acme")) == (1200, 1200)

    def test_no_logo_gets_an_initials_logo(self):
        assert _size(gv.logo_image(None, "Uri Social")) == (1200, 1200)

    def test_an_unreadable_logo_falls_back_to_initials(self):
        assert _size(gv.logo_image(b"not an image", "Uri Social")) == (1200, 1200)


class TestCopyWithinLimits:
    def test_limits(self):
        copy = gv.ad_copy("Content Creation Made Easy For Busy Founders Everywhere",
                          "Startup founders preparing to pitch can get a month of posts in a day. Try it.",
                          "A Very Long Business Name That Overflows", rsa_assets)
        assert 3 <= len(copy["headlines"]) <= 5 and all(len(h) <= 30 for h in copy["headlines"])
        assert len(copy["long_headline"]) <= 90 and copy["long_headline"].startswith("Startup founders")
        assert all(len(d) <= 90 for d in copy["descriptions"]) and len(copy["descriptions"]) >= 2
        assert len(copy["business_name"]) <= 25

    def test_youtube_and_discover_headlines_may_be_longer(self):
        copy = gv.ad_copy("Content Creation Made Easy For Founders", "Text.", "Uri", rsa_assets, headline_limit=40)
        assert copy["headlines"][0] == "Content Creation Made Easy For Founders"


def _ops(placement):
    images = {"landscape": "L", "square": "S", "portrait": "P"}
    copy = {"headlines": ["A", "B", "C"], "long_headline": "Long", "descriptions": ["D1", "D2"],
            "business_name": "Uri"}
    return gv.build_operations(placement, "123", name="URI - Uri - x", daily_micros="700000",
                               geo_constant="geoTargetConstants/2566", final_url="https://urisocial.com",
                               copy=copy, images=images, logo_b64="LOGO")


def _kinds(ops):
    return [next(iter(o)) for o in ops]


class TestOneAtomicRequest:
    @pytest.mark.parametrize("placement, channel", [("display", "DISPLAY"), ("demand_gen", "DEMAND_GEN"),
                                                    ("pmax", "PERFORMANCE_MAX")])
    def test_campaign_is_paused_and_on_the_right_channel(self, placement, channel):
        camp = next(o["campaignOperation"]["create"] for o in _ops(placement) if "campaignOperation" in o)
        assert camp["advertisingChannelType"] == channel and camp["status"] == "PAUSED"
        assert camp["containsEuPoliticalAdvertising"]

    @pytest.mark.parametrize("placement", ["display", "demand_gen", "pmax"])
    def test_every_reference_points_to_something_created_earlier(self, placement):
        made = set()
        for op in _ops(placement):
            body = next(iter(op.values()))["create"]
            refs = [v for k, v in body.items() if isinstance(v, str) and "/-" in v and k != "resourceName"]
            for key in ("ad",):
                if key in body:
                    for spec in body[key].values():
                        if isinstance(spec, list):
                            refs += [i["asset"] for i in spec if isinstance(i, dict) and "asset" in i]
            assert all(r in made for r in refs), (placement, refs)
            if "resourceName" in body:
                made.add(body["resourceName"])

    def test_display_ad_has_googles_required_pieces(self):
        ad = next(o["adGroupAdOperation"]["create"]["ad"] for o in _ops("display") if "adGroupAdOperation" in o)
        rda = ad["responsiveDisplayAd"]
        assert rda["marketingImages"] and rda["squareMarketingImages"] and rda["squareLogoImages"] and rda["longHeadline"]["text"]
        assert rda["businessName"] == "Uri" and ad["finalUrls"] == ["https://urisocial.com"]

    def test_youtube_and_discover_ad_has_a_logo_and_all_image_shapes(self):
        ad = next(o["adGroupAdOperation"]["create"]["ad"] for o in _ops("demand_gen") if "adGroupAdOperation" in o)
        dg = ad["demandGenMultiAssetAd"]
        assert dg["logoImages"] and dg["marketingImages"] and dg["squareMarketingImages"] and dg["portraitMarketingImages"]

    def test_everywhere_links_every_required_asset_to_the_asset_group(self):
        fields = [o["assetGroupAssetOperation"]["create"]["fieldType"] for o in _ops("pmax")
                  if "assetGroupAssetOperation" in o]
        for required in ("HEADLINE", "LONG_HEADLINE", "DESCRIPTION", "BUSINESS_NAME", "LOGO",
                         "MARKETING_IMAGE", "SQUARE_MARKETING_IMAGE"):
            assert required in fields
        assert fields.count("HEADLINE") >= 3 and fields.count("DESCRIPTION") >= 2

    def test_reading_back_what_google_created(self):
        out = gv.created([{"campaignBudgetResult": {"resourceName": "customers/1/campaignBudgets/9"}},
                          {"campaignResult": {"resourceName": "customers/1/campaigns/8"}},
                          {"adGroupAdResult": {"resourceName": "customers/1/adGroupAds/7~6"}}])
        assert out["campaign"].endswith("/8") and out["adGroupAd"].endswith("7~6")


def test_unknown_placement_means_search():
    assert gv.normalize_placement("tv") == "search" and gv.normalize_placement("PMAX") == "pmax"
