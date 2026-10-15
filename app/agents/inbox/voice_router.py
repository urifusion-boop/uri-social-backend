"""
Unified Inbox — Brand Voice endpoints (Personality PRD §13).

Any member of the workspace can draft, test and calibrate. Only an admin can
publish or roll back what customers actually hear (§14): on a solo workspace that
is the owner; on an agency brand, an agency admin.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency

from . import agent, voice
from .voice_catalog import (
    FEEDBACK_LABELS, SCENARIOS, SECTIONS, TEMPLATES, TRAIT_LABELS, TRAITS,
)

router = APIRouter(prefix="/inbox/personality", tags=["Unified Inbox · Brand Voice"])


def _ws(ctx: dict) -> str:
    return ctx.get("brand_id") or ""


def _raise(e: voice.VoiceError):
    raise HTTPException(status_code=e.status, detail=e.extra or e.detail) if e.extra \
        else HTTPException(status_code=e.status, detail=e.detail)


async def _can_publish(db, ctx: dict) -> bool:
    if not ctx.get("agency_id"):
        return True
    from app.services.AgencyService import AgencyService
    return await AgencyService.is_agency_admin(ctx.get("user_id", ""), ctx["agency_id"], db)


async def _require_admin(db, ctx: dict) -> None:
    if not await _can_publish(db, ctx):
        raise HTTPException(status_code=403, detail="Only an admin can publish or roll back the Brand Voice.")


async def _industry(db, workspace_id: str) -> str:
    profile = await agent._brand_profile(db, workspace_id) or {}
    return str(profile.get("industry") or profile.get("business_type") or "")


@router.get("/templates")
async def templates() -> dict:
    return {"templates": TEMPLATES, "traits": list(TRAITS), "trait_labels": TRAIT_LABELS,
            "scenarios": SCENARIOS, "feedback_labels": FEEDBACK_LABELS}


@router.get("/overview")
async def overview(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    ws = _ws(ctx)
    return {"profiles": await voice.list_profiles(db, ws), "can_publish": await _can_publish(db, ctx)}


# ── Assessment ───────────────────────────────────────────────────────────────

@router.get("/assessment")
async def get_assessment(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    ws = _ws(ctx)
    doc = await db[voice.ASSESSMENTS].find_one({"workspace_id": ws}) or {}
    return {"sections": SECTIONS,
            **voice.assessment_view(doc.get("answers") or {}, await _industry(db, ws))}


@router.post("/assessment/responses")
async def save_responses(answers: dict = Body(..., embed=True),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Autosave. Merges into what was saved, so answering one screen at a time works."""
    ws = _ws(ctx)
    doc = await db[voice.ASSESSMENTS].find_one({"workspace_id": ws}) or {}
    merged = voice.clean_answers({**(doc.get("answers") or {}), **(answers or {})})
    await db[voice.ASSESSMENTS].update_one(
        {"workspace_id": ws},
        {"$set": {"answers": merged, "updated_at": agent.now(), "updated_by": ctx.get("user_id", "")},
         "$setOnInsert": {"workspace_id": ws}}, upsert=True)
    return {"sections": SECTIONS, **voice.assessment_view(merged, await _industry(db, ws))}


@router.post("/generate")
async def generate(profile_id: Optional[str] = Body(None, embed=True),
                   db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Turn the assessment into a DRAFT profile. Clashing answers must be resolved
    first; they come back as questions."""
    ws, actor = _ws(ctx), ctx.get("user_id", "")
    doc = await db[voice.ASSESSMENTS].find_one({"workspace_id": ws}) or {}
    state = voice.assessment_view(doc.get("answers") or {}, await _industry(db, ws))
    if state["missing_required"]:
        raise HTTPException(status_code=400, detail={
            "message": "A few required questions are still unanswered.",
            "missing_required": state["missing_required"]})
    if state["conflicts"]:
        raise HTTPException(status_code=409, detail={
            "message": "Some answers pull in opposite directions. Pick which matters more.",
            "conflicts": state["conflicts"]})
    built = voice.profile_from_answers(state["answers"])
    try:
        if profile_id:
            profile = await voice.update_profile(db, ws, profile_id, built["profile"], actor,
                                                 method="assessment")
        else:
            profile = await voice.create_profile(db, ws, built["profile"], actor, "assessment")
        for ex in built["examples"]:
            prompt = {"price_enquiry": "How much is this?",
                      "price_objection": "Your price is too expensive.",
                      "delayed_order": "I've waited for my order and nobody is replying."}[ex["scenario"]]
            await voice.add_example(db, ws, f"Customer: {prompt}\nUs: {ex['text']}", "assessment",
                                    actor, scenario=ex["scenario"], approved=True)
        if built["exchange"].strip():
            try:
                await voice.add_example(db, ws, built["exchange"], "assessment", actor, approved=True)
            except voice.VoiceError:
                pass
    except voice.VoiceError as e:
        _raise(e)
    await db[voice.ASSESSMENTS].update_one({"workspace_id": ws}, {"$set": {
        "completed_at": agent.now(), "profile_id": profile["id"]}})
    return {"profile": profile}


# ── Profiles ─────────────────────────────────────────────────────────────────

@router.post("/profiles")
async def create(template_id: Optional[str] = Body(None, embed=True),
                 name: Optional[str] = Body(None, embed=True),
                 db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                 ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        fields = voice.from_template(template_id) if template_id else voice.normalize_profile({})
        if name:
            fields["name"] = name
        return {"profile": await voice.create_profile(
            db, _ws(ctx), fields, ctx.get("user_id", ""), "template" if template_id else "manual")}
    except voice.VoiceError as e:
        _raise(e)


@router.get("/profiles/{profile_id}")
async def read(profile_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
               ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return {"profile": voice.view(await voice.get_profile(db, _ws(ctx), profile_id))}
    except voice.VoiceError as e:
        _raise(e)


@router.put("/profiles/{profile_id}")
async def edit(profile_id: str, payload: dict = Body(...),
               db: AsyncIOMotorDatabase = Depends(get_db_dependency),
               ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Edits the draft. Customers keep hearing the published version until the
    next publish. Restricted rules (departments, the default) are admin-only."""
    if ({"departments", "is_default"} & set(payload)) and not await _can_publish(db, ctx):
        raise HTTPException(status_code=403, detail="Only an admin can change which profile answers which messages.")
    if isinstance(payload.get("template_id"), str) and payload["template_id"] and payload.get("apply_template"):
        try:
            tpl = voice.from_template(payload["template_id"])
        except voice.VoiceError as e:
            _raise(e)
        payload = {**payload, "traits": tpl["traits"], "language": tpl["language"]}
    try:
        return {"profile": await voice.update_profile(db, _ws(ctx), profile_id, payload,
                                                      ctx.get("user_id", ""))}
    except voice.VoiceError as e:
        _raise(e)


@router.delete("/profiles/{profile_id}")
async def archive(profile_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    await _require_admin(db, ctx)
    try:
        await voice.archive_profile(db, _ws(ctx), profile_id, ctx.get("user_id", ""))
    except voice.VoiceError as e:
        _raise(e)
    return {"status": "ok"}


@router.post("/profiles/{profile_id}/publish")
async def publish(profile_id: str, note: str = Body("", embed=True),
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    await _require_admin(db, ctx)
    try:
        return {"profile": await voice.publish(db, _ws(ctx), profile_id, ctx.get("user_id", ""), note)}
    except voice.VoiceError as e:
        _raise(e)


@router.post("/profiles/{profile_id}/rollback")
async def rollback(profile_id: str, version: int = Body(..., embed=True),
                   db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    await _require_admin(db, ctx)
    try:
        return {"profile": await voice.rollback(db, _ws(ctx), profile_id, version,
                                                ctx.get("user_id", ""))}
    except voice.VoiceError as e:
        _raise(e)


@router.get("/profiles/{profile_id}/versions")
async def versions(profile_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        ws = _ws(ctx)
        return {"versions": await voice.list_versions(db, ws, profile_id),
                "audit": await voice.audit_log(db, ws, profile_id)}
    except voice.VoiceError as e:
        _raise(e)


# ── Examples ─────────────────────────────────────────────────────────────────

@router.get("/examples")
async def examples(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    return {"examples": await voice.list_examples(db, _ws(ctx))}


@router.post("/examples")
async def add_example(text: str = Body(..., embed=True), source: str = Body("paste", embed=True),
                      scenario: str = Body("", embed=True), language: str = Body("en", embed=True),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                      ctx: dict = Depends(get_active_brand_context)) -> dict:
    ws = _ws(ctx)
    brand = (await agent._brand_profile(db, ws) or {}).get("brand_name") or ""
    try:
        return {"example": await voice.add_example(db, ws, text, source, ctx.get("user_id", ""),
                                                   scenario=scenario, language=language,
                                                   brand_name=brand)}
    except voice.VoiceError as e:
        _raise(e)


@router.post("/examples/import-inbox")
async def import_inbox(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                       ctx: dict = Depends(get_active_brand_context)) -> dict:
    added = await voice.import_from_inbox(db, _ws(ctx), ctx.get("user_id", ""))
    return {"examples": added, "added": len(added)}


@router.patch("/examples/{example_id}")
async def patch_example(example_id: str, payload: dict = Body(...),
                        db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                        ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return {"example": await voice.update_example(db, _ws(ctx), example_id, payload)}
    except voice.VoiceError as e:
        _raise(e)


@router.delete("/examples/{example_id}")
async def delete_example(example_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        await voice.delete_example(db, _ws(ctx), example_id)
    except voice.VoiceError as e:
        _raise(e)
    return {"status": "ok"}


@router.post("/analyze-examples")
async def analyze(profile_id: str = Body(..., embed=True),
                  example_ids: Optional[list[str]] = Body(None, embed=True),
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await voice.analyze_examples(db, _ws(ctx), profile_id, agent.openai_json, example_ids)
    except voice.VoiceError as e:
        _raise(e)
    except Exception as e:
        print(f"[BrandVoice] analyse failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        raise HTTPException(status_code=503, detail="Couldn't analyse the examples right now. Try again.")


@router.post("/profiles/{profile_id}/apply-analysis")
async def apply_analysis(profile_id: str, traits: dict = Body(..., embed=True),
                         db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                         ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Accept some or all of the proposed traits into the draft."""
    try:
        doc = await voice.get_profile(db, _ws(ctx), profile_id)
        merged = {**(doc.get("traits") or {}), **{k: v for k, v in (traits or {}).items() if k in TRAITS}}
        return {"profile": await voice.update_profile(db, _ws(ctx), profile_id, {"traits": merged},
                                                      ctx.get("user_id", ""), method="examples")}
    except voice.VoiceError as e:
        _raise(e)


# ── Playground ───────────────────────────────────────────────────────────────

@router.post("/preview")
async def preview(profile_id: Optional[str] = Body(None, embed=True),
                  profile: Optional[dict] = Body(None, embed=True),
                  scenario_ids: Optional[list[str]] = Body(None, embed=True),
                  message: str = Body("", embed=True),
                  kind: str = Body("dm", embed=True),
                  language: str = Body("en", embed=True),
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                  ctx: dict = Depends(get_active_brand_context)) -> dict:
    """Sample replies. `profile` previews unsaved edits; otherwise the saved draft."""
    ws = _ws(ctx)
    try:
        base: dict[str, Any] = voice.view(await voice.get_profile(db, ws, profile_id)) \
            if profile_id else {}
        fields = {**{k: base.get(k) for k in voice.EDITABLE if k in base}, **(profile or {})}
        replies = await voice.preview(db, ws, fields, agent.openai_json, scenario_ids, message,
                                      kind if kind in ("dm", "comment") else "dm",
                                      language if language in ("en", "pcm") else "en")
    except voice.VoiceError as e:
        _raise(e)
    except Exception as e:
        print(f"[BrandVoice] preview failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        raise HTTPException(status_code=503, detail="Couldn't write sample replies right now. Try again.")
    return {"replies": replies}


@router.post("/calibration")
async def calibrate(profile_id: str = Body(..., embed=True), scenario_id: str = Body(..., embed=True),
                    reply: str = Body("", embed=True), label: str = Body(..., embed=True),
                    rewrite: str = Body("", embed=True),
                    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                    ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return await voice.calibrate(db, _ws(ctx), profile_id, scenario_id, reply, label,
                                     ctx.get("user_id", ""), agent.openai_json, rewrite)
    except voice.VoiceError as e:
        _raise(e)
    except Exception as e:
        print(f"[BrandVoice] calibration failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        raise HTTPException(status_code=503, detail="Saved your rating, but couldn't redraft right now.")


@router.get("/calibration")
async def calibration_history(profile_id: str, db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                              ctx: dict = Depends(get_active_brand_context)) -> dict:
    return {"history": await voice.calibration_history(db, _ws(ctx), profile_id)}


# ── Insights ─────────────────────────────────────────────────────────────────

@router.get("/insights")
async def insights(db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                   ctx: dict = Depends(get_active_brand_context)) -> dict:
    return await voice.insights(db, _ws(ctx))


@router.post("/suggestions/{suggestion_id}")
async def decide(suggestion_id: str, accept: bool = Body(..., embed=True),
                 db: AsyncIOMotorDatabase = Depends(get_db_dependency),
                 ctx: dict = Depends(get_active_brand_context)) -> dict:
    try:
        return {"profile": await voice.decide_suggestion(db, _ws(ctx), suggestion_id, accept,
                                                         ctx.get("user_id", ""))}
    except voice.VoiceError as e:
        _raise(e)
