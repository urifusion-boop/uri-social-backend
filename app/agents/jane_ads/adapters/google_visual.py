"""
Google placements beyond Search: Display, YouTube & Discover (Demand Gen) and
Everywhere (Performance Max). Each is created in ONE GoogleAdsService.Mutate
request with temporary ids, so Google creates all of it or none of it — no
half-built campaign to roll back.

Search stays in GoogleAdsAdapter.launch_campaign; this module is what it hands
off to when the plan's placement is visual. Everything is created PAUSED, the
same as Search: nothing spends until the owner presses play.

Images come from the ad's own creative (Jane's generated image or the owner's
upload), cropped to the shapes Google asks for. The logo is the brand's own;
a brand without one gets a clean initials logo so Google's required logo slot
is never empty.
"""
from __future__ import annotations

import base64
import io
import uuid
from typing import Optional

import httpx

PLACEMENTS = ("search", "display", "demand_gen", "pmax")
VISUAL_PLACEMENTS = ("display", "demand_gen", "pmax")

PLACEMENT_LABELS = {
    "search": "Google Search",
    "display": "Google Display",
    "demand_gen": "YouTube & Discover",
    "pmax": "Google — everywhere",
}

# Google's shapes (width, height): landscape 1.91:1, square 1:1, portrait 4:5.
_LANDSCAPE = (1200, 628)
_SQUARE = (1200, 1200)
_PORTRAIT = (960, 1200)
_LOGO = (1200, 1200)


def normalize_placement(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return value if value in PLACEMENTS else "search"


# ── images ───────────────────────────────────────────────────────────────────

def _crop_to(img, size: tuple[int, int]):
    """Centre-crop to the target aspect ratio, then resize to the exact size."""
    from PIL import Image

    w, h = img.size
    target_ratio = size[0] / size[1]
    if w / h > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img.resize(size, Image.LANCZOS)


def _jpeg_b64(img) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _png_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def marketing_images(image_bytes: bytes) -> dict[str, str]:
    """The creative cropped to Google's three shapes, base64 JPEG."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return {
        "landscape": _jpeg_b64(_crop_to(img, _LANDSCAPE)),
        "square": _jpeg_b64(_crop_to(img, _SQUARE)),
        "portrait": _jpeg_b64(_crop_to(img, _PORTRAIT)),
    }


def logo_image(logo_bytes: Optional[bytes], business_name: str, color: str = "#C2185B") -> str:
    """The brand's logo on a square canvas (Google requires 1:1), or a clean
    initials logo when the brand has none."""
    from PIL import Image, ImageDraw, ImageFont

    if logo_bytes:
        try:
            logo = Image.open(io.BytesIO(logo_bytes))
            logo.load()
            logo = logo.convert("RGBA")
            canvas = Image.new("RGBA", _LOGO, (255, 255, 255, 255))
            logo.thumbnail((int(_LOGO[0] * 0.8), int(_LOGO[1] * 0.8)), Image.LANCZOS)
            canvas.paste(logo, ((_LOGO[0] - logo.width) // 2, (_LOGO[1] - logo.height) // 2), logo)
            return _png_b64(canvas.convert("RGB"))
        except Exception as e:
            print(f"[google_visual] brand logo unusable, using initials: {e}", flush=True)
    words = [w for w in (business_name or "Ad").split() if w[:1].isalnum()]
    initials = "".join(w[0] for w in words[:2]).upper() or "A"
    try:
        bg = tuple(int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        bg = (194, 24, 91)
    canvas = Image.new("RGB", _LOGO, bg)
    draw = ImageDraw.Draw(canvas)
    font = None
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"):
        try:
            font = ImageFont.truetype(name, 520)
            break
        except Exception:
            continue
    font = font or ImageFont.load_default()
    box = draw.textbbox((0, 0), initials, font=font)
    draw.text(((_LOGO[0] - (box[2] - box[0])) / 2 - box[0], (_LOGO[1] - (box[3] - box[1])) / 2 - box[1]),
              initials, fill=(255, 255, 255), font=font)
    return _png_b64(canvas)


async def fetch_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and resp.content:
            return resp.content
        print(f"[google_visual] {url} returned {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[google_visual] could not download {url}: {e}", flush=True)
    return None


# ── copy ─────────────────────────────────────────────────────────────────────

def _fit(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-—") or text[:limit]


def ad_copy(headline: str, primary_text: str, business_name: str, rsa_assets, headline_limit: int = 30) -> dict:
    """Headlines, a long headline, descriptions and the business name, within
    Google's limits. rsa_assets is the Search helper that already pads to
    Google's minimum counts with lines that fit."""
    heads, descs = rsa_assets([headline] if headline else [], [primary_text] if primary_text else [], {})
    if headline_limit > 30 and headline:
        heads = [_fit(headline, headline_limit)] + [h for h in heads if h.lower() != _fit(headline, 30).lower()]
    first_sentence = (primary_text or headline or "").split(". ")[0]
    return {
        "headlines": heads[:5],
        "long_headline": _fit(first_sentence or headline or business_name, 90),
        "descriptions": descs[:5],
        "business_name": _fit(business_name or "Our business", 25),
    }


# ── building the request ─────────────────────────────────────────────────────

class _Temp:
    """Google's temporary ids: negative numbers, unique within one request."""

    def __init__(self, customer_id: str):
        self._cid = customer_id
        self._n = 0

    def __call__(self, collection: str) -> str:
        self._n -= 1
        return f"customers/{self._cid}/{collection}/{self._n}"


def build_operations(placement: str, customer_id: str, *, name: str, daily_micros: str, geo_constant: str,
                     final_url: str, copy: dict, images: dict, logo_b64: str) -> list[dict]:
    """Every operation for one paused campaign of this placement, in the order
    Google needs them (each item may only reference ones before it)."""
    tid = _Temp(customer_id)
    budget, campaign = tid("campaignBudgets"), tid("campaigns")
    ops: list[dict] = [{"campaignBudgetOperation": {"create": {
        "resourceName": budget,
        "name": f"{name} budget {uuid.uuid4().hex[:6]}",
        "amountMicros": daily_micros,
        "deliveryMethod": "STANDARD",
        "explicitlyShared": False,
    }}}]
    camp = {
        "resourceName": campaign,
        "name": name,
        "status": "PAUSED",
        "campaignBudget": budget,
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }
    if placement == "display":
        camp.update({"advertisingChannelType": "DISPLAY", "targetSpend": {},
                     "networkSettings": {"targetGoogleSearch": False, "targetSearchNetwork": False,
                                         "targetContentNetwork": True, "targetPartnerSearchNetwork": False}})
    elif placement == "demand_gen":
        camp.update({"advertisingChannelType": "DEMAND_GEN", "targetSpend": {}})
    else:
        camp.update({"advertisingChannelType": "PERFORMANCE_MAX", "maximizeConversions": {},
                     "brandGuidelinesEnabled": False})
    ops.append({"campaignOperation": {"create": camp}})
    ops.append({"campaignCriterionOperation": {"create": {
        "campaign": campaign, "location": {"geoTargetConstant": geo_constant}}}})

    def image_asset(kind: str, data: str) -> str:
        res = tid("assets")
        ops.append({"assetOperation": {"create": {
            "resourceName": res, "name": f"{name} {kind} {uuid.uuid4().hex[:6]}",
            "imageAsset": {"data": data}}}})
        return res

    landscape = image_asset("landscape", images["landscape"])
    square = image_asset("square", images["square"])
    logo = image_asset("logo", logo_b64)
    portrait = image_asset("portrait", images["portrait"]) if placement in ("demand_gen", "pmax") else ""

    if placement in ("display", "demand_gen"):
        ad_group = tid("adGroups")
        group = {"resourceName": ad_group, "name": f"{name} ad group", "campaign": campaign, "status": "ENABLED"}
        if placement == "display":
            group["type"] = "DISPLAY_STANDARD"
        ops.append({"adGroupOperation": {"create": group}})
        texts = lambda items: [{"text": t} for t in items]  # noqa: E731
        if placement == "display":
            ad = {"responsiveDisplayAd": {
                "marketingImages": [{"asset": landscape}],
                "squareMarketingImages": [{"asset": square}],
                # Display's logoImages slot is the 4:1 landscape logo; a square logo goes here.
                "squareLogoImages": [{"asset": logo}],
                "headlines": texts(copy["headlines"]),
                "longHeadline": {"text": copy["long_headline"]},
                "descriptions": texts(copy["descriptions"]),
                "businessName": copy["business_name"],
            }}
        else:
            ad = {"demandGenMultiAssetAd": {
                "marketingImages": [{"asset": landscape}],
                "squareMarketingImages": [{"asset": square}],
                "portraitMarketingImages": [{"asset": portrait}],
                "logoImages": [{"asset": logo}],
                "headlines": texts(copy["headlines"]),
                "descriptions": texts(copy["descriptions"]),
                "businessName": copy["business_name"],
            }}
        ad["finalUrls"] = [final_url]
        ops.append({"adGroupAdOperation": {"create": {"adGroup": ad_group, "status": "ENABLED", "ad": ad}}})
        return ops

    # Performance Max: one asset group holding every asset.
    def text_asset(text: str) -> str:
        res = tid("assets")
        ops.append({"assetOperation": {"create": {"resourceName": res, "textAsset": {"text": text}}}})
        return res

    links: list[tuple[str, str]] = []
    links += [(text_asset(h), "HEADLINE") for h in copy["headlines"][:5]]
    links.append((text_asset(copy["long_headline"]), "LONG_HEADLINE"))
    links += [(text_asset(d), "DESCRIPTION") for d in copy["descriptions"][:5]]
    links.append((text_asset(copy["business_name"]), "BUSINESS_NAME"))
    links += [(landscape, "MARKETING_IMAGE"), (square, "SQUARE_MARKETING_IMAGE"),
              (portrait, "PORTRAIT_MARKETING_IMAGE"), (logo, "LOGO")]
    asset_group = tid("assetGroups")
    ops.append({"assetGroupOperation": {"create": {
        "resourceName": asset_group, "name": f"{name} assets", "campaign": campaign,
        "finalUrls": [final_url], "status": "ENABLED"}}})
    ops += [{"assetGroupAssetOperation": {"create": {"assetGroup": asset_group, "asset": asset, "fieldType": field}}}
            for asset, field in links]
    return ops


def created(responses: list[dict]) -> dict:
    """Resource names Google gave back, by kind (first of each)."""
    out: dict[str, str] = {}
    for r in responses:
        for key, value in r.items():
            kind = key.replace("Result", "")
            if isinstance(value, dict) and value.get("resourceName") and kind not in out:
                out[kind] = value["resourceName"]
    return out
