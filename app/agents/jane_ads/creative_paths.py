"""
Jane + Ads — the creative production paths (Ads Intelligence PRD §7.5, CR-02/03/06/07).

A campaign's media comes down one of these paths, and every one is optional:

  static     an image Jane designs or generates
  upload     the business's own photo or footage, used as-is
  ai_video   a video made in URI's AI video studio
  ugc        a person on camera talking about the business — a real customer, a
             paid creator, a creator-style brand video, or the owner/staff

The brief's constraints decide which paths are open, and a path the client has
ruled out is never silently substituted (CR-03). AI video and UGC are never
recommended by default; UGC is suggested only where the buying decision rests on
trust, demonstration or lived experience (§7.5 "When UGC is appropriate").

Honesty rules (CR-07): what a UGC video IS travels with the plan, and copy that
calls someone a customer is blocked unless they are one. A paid creator is
disclosed in the ad itself. Synthetic video is labelled synthetic.
"""
from __future__ import annotations

import re
from typing import Optional

PATHS = ("static", "upload", "ai_video", "ugc")

UGC_KINDS = {
    "real_customer": {
        "label": "A real customer",
        "honest_as": "a real customer talking about you, with their permission",
        "disclosure": None,
    },
    "paid_creator": {
        "label": "A paid creator",
        "honest_as": "a creator you paid or gifted — disclosed in the ad",
        "disclosure": "Paid partnership",
    },
    "creator_style": {
        "label": "Creator-style brand video",
        "honest_as": "your own brand video in a creator style — never presented as a customer",
        "disclosure": None,
    },
    "staff_demo": {
        "label": "Me or my team",
        "honest_as": "the owner or staff showing the product — not a customer",
        "disclosure": None,
    },
}

# Who appears on camera, handed to the shoot-script writer so the shots describe the
# right person — never the owner in a creator's or a customer's video.
FILMED_BY = {
    "real_customer": "A real, existing customer films themselves using or receiving the product and says "
                     "what they think in their own words. The owner does not appear. Do not script their opinion.",
    "paid_creator": "A paid creator (not the owner, not presented as a customer) films themselves trying the "
                    "product in their own style, on their phone.",
    "creator_style": "A presenter films a creator-style video for the brand, as the brand's own content — "
                     "not the owner and never presented as a customer.",
    "staff_demo": "The owner or a member of staff films themselves showing and explaining the product.",
}

# Wording that presents the speaker as a customer. Only a real customer's video
# may carry it.
_CUSTOMER_CLAIM = re.compile(
    r"\b(our|a|one of our|happy|real|satisfied|loyal)\s+(customer|client|buyer)s?\b"
    r"|\b(customer|client)\s+(review|testimonial|story|says|said)\b"
    r"|\btestimonial\b|\bhear from (our|a) (customer|client)s?\b",
    re.IGNORECASE,
)

# Categories where the buying decision leans on trust, demonstration or lived
# experience — the PRD's "good candidates" for UGC.
_UGC_FIT = re.compile(
    r"skin|beauty|cosmetic|makeup|hair|salon|spa|fashion|cloth|wear|boutique|shoe|food|restaurant|"
    r"kitchen|cater|bak|chef|app|software|course|class|coach|gym|fitness|clinic|service|repair|"
    r"clean|tutor|school|travel|hotel",
    re.IGNORECASE,
)

_PERSON_WORDS = re.compile(
    r"\b(person|people|man|men|woman|women|girl|boy|child|kid|customer|chef|presenter|host|"
    r"face|smil\w*|talking|speaks?|model|influencer|creator|owner|staff|worker|family|friends?)\b",
    re.IGNORECASE,
)
_AVATAR_MODELS = ("ai-avatar", "lip-sync")


def claims_customer(text: str) -> bool:
    return bool(_CUSTOMER_CLAIM.search(text or ""))


def video_has_people(job: dict) -> bool:
    """Whether a generated video shows people: a talking-avatar clip always does;
    otherwise each clip's own prompt is the only record of what it shows."""
    for clip in job.get("clips") or []:
        if any(m in (clip.get("routed_model") or "") for m in _AVATAR_MODELS):
            return True
        if _PERSON_WORDS.search(clip.get("video_prompt") or ""):
            return True
    return False


def availability(brief: dict) -> dict:
    """Which paths this brand's brief leaves open, and why any is closed."""
    rules = set(brief.get("constraints") or [])
    closed = {}
    if "own_photos_only" in rules:
        closed["static"] = "You asked for your own photos only, so Jane won't generate images."
        closed["ai_video"] = "You asked for your own photos only."
    if "no_ai_video" in rules:
        closed["ai_video"] = "You asked for no AI-generated video."
    if "no_ugc" in rules:
        closed["ugc"] = "You asked for no creator or UGC-style content."
    out = {p: {"open": p not in closed, "why_closed": closed.get(p)} for p in PATHS}
    if "no_generated_people" in rules and out["ai_video"]["open"]:
        out["ai_video"]["note"] = "Only AI videos without people — no presenters or faces."
    return out


def recommend(category: str, description: str, brief: dict, has_own_media: bool = False) -> dict:
    """The path Jane suggests first, with the reason. Never AI video by default;
    UGC only where it genuinely fits; a closed path is never suggested."""
    open_paths = availability(brief)
    text = f"{category} {description}"
    ugc_fits = bool(_UGC_FIT.search(text))
    if has_own_media and open_paths["upload"]["open"]:
        primary, why = "upload", "Your own photos and footage show the real thing, and cost nothing to make."
    elif open_paths["static"]["open"]:
        primary, why = "static", "A clear image is the quickest, cheapest way to test what message works."
    else:
        primary, why = "upload", "Upload your own photos — your brief rules out generated images."
    ugc = None
    if open_paths["ugc"]["open"]:
        ugc = ({"suggested": True,
                "why": "People buy this on trust and seeing it used, so a real person on camera can help. "
                       "If you have no customer video, a creator brief or a video of you or your team works "
                       "— never an invented customer."}
               if ugc_fits else
               {"suggested": False,
                "why": "This is better shown clearly than explained by a person, so UGC isn't the first thing "
                       "to try."})
    return {"primary": primary, "why": why, "ugc": ugc, "paths": open_paths,
            "ai_video_note": "AI video is available if you want motion, but it's never required."
            if open_paths["ai_video"]["open"] else None}


def provenance(path: str, *, ugc_kind: Optional[str] = None, rights_confirmed: bool = False,
               video_job_id: Optional[str] = None, has_people: Optional[bool] = None) -> dict:
    """What this creative IS, carried on the plan so checks and reports can see it."""
    if path not in PATHS:
        raise ValueError(f"unknown creative path {path!r}")
    out: dict = {"path": path, "synthetic": path == "ai_video"}
    if path == "ai_video":
        out.update(video_job_id=video_job_id, has_people=bool(has_people))
    if path == "ugc":
        if ugc_kind not in UGC_KINDS:
            raise ValueError(f"unknown UGC kind {ugc_kind!r}")
        out.update(ugc_kind=ugc_kind, rights_confirmed=bool(rights_confirmed),
                   honest_as=UGC_KINDS[ugc_kind]["honest_as"],
                   disclosure=UGC_KINDS[ugc_kind]["disclosure"])
    return out


def disclose(primary_text: str, prov: Optional[dict]) -> str:
    """A paid creator is disclosed in the ad's own words, not left to a footnote."""
    tag = (prov or {}).get("disclosure")
    if not tag or tag.lower() in (primary_text or "").lower():
        return primary_text
    return f"{primary_text.rstrip()}\n\n{tag}."


def checks(plan: dict, brief: dict) -> list[dict]:
    """Plan-time checks for the path a creative came down (feeds brief.check_plan)."""
    prov = (plan.get("creative") or {}).get("provenance") or {}
    path = prov.get("path")
    if not path:
        return []
    out = []
    rules = set(brief.get("constraints") or [])
    if path == "ai_video":
        if "no_ai_video" in rules or "own_photos_only" in rules:
            out.append({"code": "constraint_no_ai_video", "level": "block",
                        "message": "You asked for no AI-generated video. Use your own footage or a photo instead."})
        elif "no_generated_people" in rules and prov.get("has_people"):
            out.append({"code": "constraint_no_generated_people", "level": "block",
                        "message": "This AI video shows people, and you asked for no AI-generated people. "
                                   "Pick a video of just the product or place."})
    if path == "ugc":
        if "no_ugc" in rules:
            out.append({"code": "constraint_no_ugc", "level": "block",
                        "message": "You asked for no creator or UGC-style content. Use a different video."})
        if not prov.get("rights_confirmed"):
            out.append({"code": "ugc_rights_missing", "level": "block",
                        "message": "Confirm the person in this video agreed to it being used in an ad."})
    creative = plan.get("creative") or {}
    copy = f"{creative.get('headline', '')} {creative.get('primary_text', '')}"
    not_a_customer = (path == "ai_video" or (path == "ugc" and prov.get("ugc_kind") != "real_customer"))
    if not_a_customer and claims_customer(copy):
        who = "an AI-generated video" if path == "ai_video" else UGC_KINDS[prov["ugc_kind"]]["honest_as"]
        out.append({"code": "misleading_customer_claim", "level": "block",
                    "message": f"The ad text presents this as a customer speaking, but it's {who}. "
                               "Edit the text so it doesn't call them a customer."})
    return out


def creator_brief(business_name: str, category: str, description: str, offer: str = "",
                  ugc_kind: str = "creator_style", shots: Optional[list[dict]] = None) -> dict:
    """CR-06: a brief a creator, customer or staff member can film from — message,
    proof, required shots, disclosure and usage rights."""
    kind = UGC_KINDS.get(ugc_kind) or UGC_KINDS["creator_style"]
    what = category or "the product"
    default_shots = [
        {"shot": "Hook (first 3 seconds)", "direction": f"Show {what} in use straight away — no logo intro."},
        {"shot": "Close-up", "direction": "The detail that proves quality: texture, finish, packaging or the result."},
        {"shot": "In use", "direction": "A real person using it in a real place in Nigeria, in natural light."},
        {"shot": "Proof", "direction": "Something checkable: the price, the before/after, the delivery arriving."},
        {"shot": "Ask", "direction": "Say exactly how to order — 'Send us a message on WhatsApp'."},
    ]
    return {
        "for": kind["label"],
        "message": f"Why {business_name or 'this business'} is worth it, in the speaker's own words"
                   + (f" — the offer: {offer}" if offer else
                      f" — about: {description.strip()[:160]}" if description.strip() else ""),
        "proof": "Show, don't claim: the product itself, a result, or a price on screen. "
                 "No figures or results that aren't real.",
        "shots": shots or default_shots,
        "length": "15–30 seconds, vertical (9:16), captions on",
        "honesty": f"This video is {kind['honest_as']}. Don't script customer reviews or put words in "
                   "anyone's mouth; let them say it their way.",
        "disclosure": (f"Show '{kind['disclosure']}' in the ad and say so in the video." if kind["disclosure"]
                       else "No paid-partnership label needed for this kind of video."),
        "usage_rights": "Get written permission (a WhatsApp message is fine) to use the video in paid ads "
                        "on Facebook and Instagram, for at least 3 months.",
        "brief_source": (description or "")[:300],
    }


VIDEO_JOBS = "video_generation_jobs"


async def ai_videos(db, user_id: str, limit: int = 20) -> list[dict]:
    """The user's finished AI studio videos, as ad-ready choices."""
    rows = await db[VIDEO_JOBS].find(
        {"user_id": user_id, "status": "complete"}, {"_id": 0, "billing": 0},
    ).sort("_id", -1).to_list(length=limit)
    out = []
    for job in rows:
        clips = [c for c in job.get("clips") or [] if c.get("video_url")]
        if not clips:
            continue
        out.append({"video_job_id": job["job_id"], "clips": len(clips),
                    "seconds": sum(int(c.get("duration_seconds") or 0) for c in clips),
                    "preview_url": job.get("merged_video_url") or clips[0]["video_url"],
                    "has_people": video_has_people(job),
                    "about": next((c.get("text_overlay") or c.get("video_prompt") or "" for c in clips), "")[:140]})
    return out


async def resolve_ai_video(db, user_id: str, job_id: str) -> tuple[str, dict]:
    """The single video file for one of the user's finished AI videos (its clips merged
    once and remembered), plus the job, for provenance. Raises LookupError when the job
    isn't theirs or isn't finished."""
    job = await db[VIDEO_JOBS].find_one({"job_id": job_id, "user_id": user_id}, {"_id": 0})
    if not job or job.get("status") != "complete":
        raise LookupError("That AI video isn't finished, or isn't yours.")
    if job.get("merged_video_url"):
        return job["merged_video_url"], job
    urls = [c["video_url"] for c in job.get("clips") or [] if c.get("video_url")]
    if not urls:
        raise LookupError("That AI video has no finished clips.")
    if len(urls) == 1:
        merged = urls[0]
    else:
        from app.agents.social_media_manager.services.video_merge_service import VideoMergeService
        merged = await VideoMergeService.merge_clips(urls)
    await db[VIDEO_JOBS].update_one({"job_id": job_id}, {"$set": {"merged_video_url": merged}})
    return merged, job
