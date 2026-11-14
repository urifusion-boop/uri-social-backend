# app/agents/social_media_manager/services/approval_workflow_service.py

from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
import asyncio
import base64
import re
import requests

from app.domain.responses.uri_response import UriResponse
from app.services.FacebookService import FacebookService
from app.core.config import settings
from app.agents.social_media_manager.services.x_direct_service import XDirectService
from pymongo import ReturnDocument


# ── Publish-failure helpers: plain-language errors, and marking a connection disconnected ──
_FRIENDLY_FACEBOOK_ERROR_PATTERNS = [
    (
        re.compile(r"confirm your identity", re.IGNORECASE),
        "Facebook is asking you to confirm your identity before you can post from this Page. "
        "This is a security check on Facebook's side, not a problem with your connection "
        "here, so reconnecting won't fix it. Open the Facebook app on your phone (signed in "
        "as the person who connected this Page) and follow the \u201cConfirm your identity\u201d "
        "prompt; it may also be in your notifications. Once Facebook has confirmed you, "
        "publish this post again."
    ),
    (
        # Live-reported: "MEEQAT TRAVELS AND TOURS LTD"'s Facebook post failed
        # with Facebook's own "400 - The user must be an administrator, editor,
        # or moderator of the page in order to impersonate it." The connected
        # Facebook user lost their admin/editor/moderator role on the Page
        # itself (removed, role changed, Page ownership changed) — the token
        # can still be technically valid, so this is NOT the same thing as a
        # dead session, and telling the owner to just "reconnect Facebook"
        # alone is misleading: reconnecting with the same now-demoted account
        # hits this identical error again. The real fix is restoring that
        # person's role on the Page, or connecting a different account that
        # already has one.
        re.compile(r"administrator,?\s*editor,?\s*or moderator of the page", re.IGNORECASE),
        "Facebook says the connected account is no longer an admin, editor or moderator of "
        "this Page, so it can't post there. Ask whoever manages the Page to restore that "
        "access, or go to Connected Accounts and reconnect Facebook using an account that "
        "already has it."
    ),
]

_TOKEN_INVALIDATION_PATTERNS = [
    re.compile(r"error validating access token", re.IGNORECASE),
    re.compile(r"session has been invalidated", re.IGNORECASE),
    re.compile(r"session is invalid", re.IGNORECASE),
    re.compile(r"administrator,?\s*editor,?\s*or moderator of the page", re.IGNORECASE),
]

UNKNOWN_FAILURE_ESCALATION_THRESHOLD = 3

def _friendlier_facebook_error(raw: Optional[str]) -> str:
    """Plain-language explanation for a Facebook error, keeping the
    raw detail alongside it (support/debugging). Checks the specific-cause
    table first so a signature with its own precise explanation (e.g. lost
    Page admin rights) never gets the generic "session expired" message
    instead — that was wrong for that case and told the owner to do
    something that wouldn't actually fix it. An error we haven't catalogued
    at all still gets a plain-language lead-in, not raw passthrough — a
    one-off failure on this post is told immediately, in words a non-
    technical owner can read, even on the first occurrence; see
    _should_flag_connection for the separate, more cautious decision about
    whether the whole connection needs reconnecting."""
    raw = raw or "unknown error"
    for pattern, friendly in _FRIENDLY_FACEBOOK_ERROR_PATTERNS:
        if pattern.search(raw):
            return f"{friendly} (Facebook's detail: {raw})"
    if _is_token_invalidation_error(raw):
        return (
            "Your Facebook connection has expired or was revoked — this Page needs to be "
            "reconnected before it can publish again. Go to Connected Accounts and reconnect "
            f"Facebook. (Facebook's detail: {raw})"
        )
    return f"This post couldn't be published to Facebook. Facebook's detail: {raw}"

def _is_token_invalidation_error(raw: Optional[str]) -> bool:
    """True when an error message signature means the connection can no longer
    publish and needs the owner's attention (reconnecting, or fixing Page
    access on Facebook's side), as opposed to a one-off content/policy
    rejection. Used to decide whether to mark a connection disconnected and
    notify the owner."""
    raw = raw or ""
    return any(pattern.search(raw) for pattern in _TOKEN_INVALIDATION_PATTERNS)

async def _mark_connection_disconnected_and_notify(
    db: AsyncIOMotorDatabase,
    user_id: Optional[str],
    platform: str,
    connection: Optional[Dict[str, Any]],
    reason: str,
) -> None:
    """Shared by every place that discovers a connection's token/session is
    dead — the immediate-publish exception path, the scheduled-poll failure
    branch, and the published-post reconciliation sweep. Without this, a dead
    connection stays connection_status="active" forever and every future
    publish attempt just fails silently again, exactly as it did for "Vchain"'s
    Facebook Page (confirmed live via CloudWatch: repeated "Error validating
    access token" failures, connection never marked disconnected, user never
    notified)."""
    if not user_id or db is None:
        return
    try:
        # The exact connection when the caller has it, else the user's connection for the platform.
        match: Dict[str, Any] = _connection_match(user_id, platform, connection)
        await db["social_connections"].update_one(
            match,
            {"$set": {"connection_status": "disconnected", "updated_at": datetime.utcnow()}},
        )
        print(f"⚠️ Marked {platform} connection disconnected for user_id={user_id} ({reason})")
    except Exception as mark_err:
        print(f"⚠️ Failed to mark connection disconnected: {mark_err}")

    try:
        from app.services.NotificationService import notification_service
        await notification_service.notify_connection_disconnected(user_id=user_id, platform=platform)
    except Exception as notify_err:
        print(f"⚠️ connection_disconnected notification failed: {notify_err}")

def _connection_match(user_id: Optional[str], platform: str, connection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if connection and connection.get("id"):
        return {"id": connection["id"]}
    return {"user_id": user_id, "platform": (connection or {}).get("platform", platform)}

async def _should_flag_connection(
    db: AsyncIOMotorDatabase,
    user_id: Optional[str],
    platform: str,
    connection: Optional[Dict[str, Any]],
    raw_error: Optional[str],
) -> tuple[bool, str]:
    """Whether a publish failure means this connection needs the owner's
    attention right now, and why. A recognized signature (dead session, lost
    Page role, ...) escalates immediately — see _is_token_invalidation_error.
    An error we have never catalogued does NOT just get ignored forever: it
    increments a per-connection counter, and after
    UNKNOWN_FAILURE_ESCALATION_THRESHOLD consecutive failures of any kind
    (known or not), it escalates too, with the raw error attached since
    there's no friendly translation for it yet. A single one-off failure
    (a bad image, a transient network hiccup) does not escalate on its own
    — only a real, repeated pattern does, the same bar a human reading the
    logs would use before concluding "this connection is actually broken."
    """
    if _is_token_invalidation_error(raw_error):
        return True, "token invalidated (recognized error signature)"
    if db is None or not user_id:
        return False, ""
    try:
        updated = await db["social_connections"].find_one_and_update(
            _connection_match(user_id, platform, connection),
            {"$inc": {"consecutive_publish_failures": 1}, "$set": {"last_publish_error": raw_error or "", "updated_at": datetime.utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
    except Exception as e:
        print(f"⚠️ _should_flag_connection: counter update failed: {e}")
        return False, ""
    count = (updated or {}).get("consecutive_publish_failures", 0)
    if count >= UNKNOWN_FAILURE_ESCALATION_THRESHOLD:
        return True, f"{count} consecutive unrecognized publish failures (latest: {(raw_error or '')[:200]})"
    return False, ""

async def _record_publish_success(
    db: AsyncIOMotorDatabase,
    user_id: Optional[str],
    platform: str,
    connection: Optional[Dict[str, Any]],
) -> None:
    """Resets the unrecognized-failure counter on an actual successful
    publish, so a connection that struggled once and then recovered doesn't
    carry a stale count toward a future, unrelated run of failures."""
    if db is None or not user_id:
        return
    try:
        await db["social_connections"].update_one(
            _connection_match(user_id, platform, connection),
            {"$set": {"consecutive_publish_failures": 0}},
        )
    except Exception as e:
        print(f"⚠️ _record_publish_success: counter reset failed: {e}")


class ApprovalWorkflowService:
    """
    Complete approval and scheduling workflow for social media content
    
    This service handles:
    - Content approval/denial workflow
    - Content refinement and regeneration
    - Scheduling for future posting
    - Automated publishing
    - Content status tracking
    """
    
    @staticmethod
    async def submit_for_approval(
        db: AsyncIOMotorDatabase,
        user_id: str,
        content_request_id: str,
        selected_drafts: List[str]  # Draft IDs to submit
    ) -> Dict[str, Any]:
        """
        Submit selected content drafts for approval
        
        Moves drafts from 'draft' to 'pending_approval' status
        """
        try:
            # Update selected drafts to pending approval
            result = await db["content_drafts"].update_many(
                {
                    "id": {"$in": selected_drafts},
                    "request_id": content_request_id,
                    "status": "draft"
                },
                {
                    "$set": {
                        "status": "pending_approval",
                        "submitted_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            
            if result.modified_count > 0:
                # Update request status
                await db["content_requests"].update_one(
                    {"id": content_request_id, "user_id": user_id},
                    {"$set": {"status": "pending_approval", "updated_at": datetime.utcnow()}}
                )
                
                return UriResponse.get_single_data_response("approval_submission", {
                    "request_id": content_request_id,
                    "submitted_drafts": selected_drafts,
                    "drafts_count": result.modified_count,
                    "status": "pending_approval",
                    "submitted_at": datetime.utcnow().isoformat()
                })
            else:
                return UriResponse.error_response("No valid drafts found for approval")
                
        except Exception as e:
            return UriResponse.error_response(f"Approval submission failed: {str(e)}")
    
    @staticmethod
    async def approve_content(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_ids: List[str],
        schedule_option: str = "immediate",  # immediate, schedule, save_draft
        scheduled_datetime: Optional[datetime] = None,
        approval_notes: Optional[str] = None,
        tiktok_settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Approve content drafts and optionally schedule them

        Args:
            draft_ids: List of draft IDs to approve
            schedule_option: 'immediate', 'schedule', or 'save_draft'
            scheduled_datetime: When to publish (if schedule_option is 'schedule')
            approval_notes: Optional notes from approver
            tiktok_settings: What the user chose on the Post to TikTok screen. Required to schedule a
                TikTok image / carousel post (TikTok allows no default), saved on the draft for the
                scheduler to post with.
        """
        try:
            print(f"✅ approve_content | user_id={user_id} draft_ids={draft_ids} schedule_option={schedule_option}")
            approved_drafts = []
            errors = []
            # Distinct from errors: the draft IS genuinely scheduled (status
            # stays "scheduled", the cron will still retry at send time) but
            # something about the initial dispatch attempt is worth flagging
            # now rather than leaving the user to find out only when/if the
            # later retry also fails. errors[] is reserved for "this draft
            # was not accepted at all" — conflating the two would make the
            # frontend show a hard failure toast for a post that actually is
            # queued, which is its own kind of misleading.
            warnings = []
            # Scheduled TikTok photo posts: published by the scheduler with the saved choices.
            scheduled_tiktok_photos = set()

            for draft_id in draft_ids:
                try:
                    # Get draft details
                    draft = await db["content_drafts"].find_one({"id": draft_id})
                    if not draft:
                        errors.append({"draft_id": draft_id, "error": "Draft not found"})
                        continue
                    
                    # Update draft status
                    update_data = {
                        "status": "approved",
                        "approved_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow()
                    }
                    
                    if approval_notes:
                        update_data["approval_notes"] = approval_notes
                    
                    if schedule_option == "schedule":
                        # Validate before accepting — fail fast with a visible error.
                        if draft.get("platform") == "instagram" and draft.get("post_type", "feed") not in ("text", "carousel"):
                            # Live-reported bug: an uploaded video (post_type stays "feed" —
                            # the upload form has no "reel" option) was rejected here because
                            # this only ever checked image_url, never video_url, even though
                            # upload_user_content correctly sets video_url (and leaves
                            # image_url None) for a video upload. A real video satisfies
                            # Instagram's "needs visual media" requirement just as much as an
                            # image does — only reject when BOTH are missing.
                            raw_img = ApprovalWorkflowService._resolve_image_url(draft.get("image_url") or "")
                            has_video = bool(draft.get("video_url"))
                            if not raw_img and not has_video:
                                errors.append({"draft_id": draft_id, "error": "Instagram requires an image or video. Add one to this post before scheduling."})
                                continue
                        elif draft.get("platform") == "instagram" and draft.get("post_type") == "carousel":
                            if not draft.get("slides"):
                                errors.append({"draft_id": draft_id, "error": "Instagram carousel requires slides. Re-generate the post."})
                                continue

                        if draft.get("platform") == "tiktok" and not draft.get("video_url"):
                            from app.agents.social_media_manager.services import tiktok_media_proxy
                            from app.agents.social_media_manager.services.tiktok_direct_service import schedule_problem
                            problem = schedule_problem(tiktok_settings, len(tiktok_media_proxy.draft_image_sources(draft)))
                            if problem:
                                errors.append({"draft_id": draft_id, "error": problem})
                                continue
                            scheduled_tiktok_photos.add(draft_id)

                        # Cancel only "publishing"/"ready_to_publish" stuck states for this
                        # user+platform — NOT already-scheduled posts for other dates.
                        # Users should be able to queue multiple posts for different times.
                        # The cron deduplicates at publish time; we only need to clear states
                        # that indicate a post is actively in-flight right now.
                        cancel_result = await db["content_drafts"].update_many(
                            {
                                "user_id": user_id,
                                "platform": draft["platform"],
                                "status": {"$in": ["publishing", "ready_to_publish"]},
                                "id": {"$ne": draft_id},
                            },
                            {"$set": {"status": "replaced", "error_message": "Superseded by a newer scheduled post.", "updated_at": datetime.utcnow()}},
                        )
                        if cancel_result.modified_count:
                            print(f"🗑️ Cancelled {cancel_result.modified_count} in-flight {draft['platform']} drafts for user {user_id}")

                        update_data["scheduled_date"] = scheduled_datetime or (datetime.utcnow() + timedelta(hours=1))
                        # On non-production environments use a staging-specific status so the
                        # production cron (which only queries "scheduled") cannot claim and
                        # double-publish staging drafts. Both staging and prod share a DB.
                        import os as _os
                        _app_env = _os.getenv("APP_ENV", "production")
                        update_data["status"] = "scheduled" if _app_env == "production" else "staging_scheduled"
                        update_data["user_id"] = user_id
                        update_data["platform_post_id"] = None  # reset so cron picks it up fresh
                        if draft_id in scheduled_tiktok_photos:
                            update_data["tiktok_settings"] = tiktok_settings
                    elif schedule_option == "immediate":
                        update_data["status"] = "ready_to_publish"

                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": update_data}
                    )

                    approved_drafts.append({
                        "draft_id": draft_id,
                        "platform": draft["platform"],
                        "post_type": draft.get("post_type", "feed"),
                        "status": update_data["status"],
                        "scheduled_date": scheduled_datetime.isoformat() if scheduled_datetime else None,
                        "brand_id": draft.get("brand_id"),
                    })

                except Exception as e:
                    errors.append({"draft_id": draft_id, "error": str(e)})

            # ── Immediate publish ────────────────────────────────────────────────
            if schedule_option == "immediate" and approved_drafts:
                publish_results = await ApprovalWorkflowService._trigger_immediate_publishing(
                    db, user_id, [d["draft_id"] for d in approved_drafts],
                    scheduled_datetime=None,
                )
                for draft in approved_drafts:
                    draft_result = publish_results.get(draft["draft_id"])
                    if draft_result:
                        draft["publish_result"] = draft_result

            # Scheduled posts stay status=scheduled and are published by the scheduler
            # (publish_scheduled_content) when their time comes, on every platform.

            return UriResponse.get_single_data_response("content_approval", {
                "approved_drafts": approved_drafts,
                "errors": errors,
                "warnings": warnings,
                "schedule_option": schedule_option,
                "scheduled_datetime": scheduled_datetime.isoformat() if scheduled_datetime else None,
                "approved_at": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            return UriResponse.error_response(f"Content approval failed: {str(e)}")
    
    @staticmethod
    async def deny_content(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_ids: List[str],
        denial_reason: str,
        request_regeneration: bool = False
    ) -> Dict[str, Any]:
        """
        Deny content drafts with optional regeneration request
        """
        try:
            denied_drafts = []
            
            for draft_id in draft_ids:
                # Update draft status
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {
                        "$set": {
                            "status": "denied",
                            "denial_reason": denial_reason,
                            "denied_at": datetime.utcnow(),
                            "updated_at": datetime.utcnow(),
                            "regeneration_requested": request_regeneration
                        }
                    }
                )
                
                denied_drafts.append(draft_id)
            
            return UriResponse.get_single_data_response("content_denial", {
                "denied_drafts": denied_drafts,
                "denial_reason": denial_reason,
                "regeneration_requested": request_regeneration,
                "denied_at": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            return UriResponse.error_response(f"Content denial failed: {str(e)}")
    
    @staticmethod
    async def refine_content(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_id: str,
        refinements: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Apply user refinements to content
        
        Args:
            refinements: {
                'content': 'Updated content text',
                'hashtags': ['new', 'hashtags'],
                'media_urls': ['url1', 'url2'],
                'refinement_notes': 'What was changed'
            }
        """
        try:
            # Get current draft
            draft = await db["content_drafts"].find_one({"id": draft_id})
            if not draft:
                return UriResponse.error_response("Draft not found")
            
            # Track changes
            human_edits = draft.get("human_edits", {})
            current_edit_count = draft.get("edit_count", 0)
            
            # Record what changed
            changes = {}
            if refinements.get('content') and refinements['content'] != draft.get('content'):
                changes['content'] = {
                    'old': draft.get('content'),
                    'new': refinements['content']
                }
            
            if refinements.get('hashtags') and refinements['hashtags'] != draft.get('hashtags'):
                changes['hashtags'] = {
                    'old': draft.get('hashtags', []),
                    'new': refinements['hashtags']
                }
            
            # Update draft
            update_data = {
                "updated_at": datetime.utcnow(),
                "edit_count": current_edit_count + 1,
                "last_edited_at": datetime.utcnow(),
                "status": "refined"  # Mark as refined, needs re-approval
            }
            
            if refinements.get('content'):
                update_data['content'] = refinements['content']
            if refinements.get('hashtags'):
                update_data['hashtags'] = refinements['hashtags']
            if refinements.get('media_urls'):
                update_data['media_urls'] = refinements['media_urls']
            if refinements.get('refinement_notes'):
                update_data['refinement_notes'] = refinements['refinement_notes']
            
            # Update human edits tracking
            human_edits[str(datetime.utcnow().timestamp())] = {
                'changes': changes,
                'notes': refinements.get('refinement_notes'),
                'timestamp': datetime.utcnow().isoformat()
            }
            update_data['human_edits'] = human_edits
            
            await db["content_drafts"].update_one(
                {"id": draft_id},
                {"$set": update_data}
            )

            # Return the full updated draft so the frontend can update its local state
            updated_draft = await db["content_drafts"].find_one({"id": draft_id})
            if updated_draft:
                updated_draft.pop("_id", None)

            return UriResponse.get_single_data_response("content_refinement", updated_draft or {
                "draft_id": draft_id,
                "changes_made": changes,
                "edit_count": current_edit_count + 1,
                "status": "refined",
                "refined_at": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            return UriResponse.error_response(f"Content refinement failed: {str(e)}")
    
    @staticmethod
    async def regenerate_content(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_id: str,
        regeneration_feedback: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Regenerate content based on feedback

        PRD Credit Logic:
        - First retry (retry_count=1): FREE (PRD 3.2)
        - Second retry (retry_count=2): Costs 1 credit (PRD 3.2)
        - PRD 3.3: System must show "This action will use 1 credit. Continue?" before second retry
        """
        try:
            from .content_generation_service import ContentGenerationService
            from app.services.CreditService import credit_service
            from app.services.TrialService import trial_service

            # Get original draft details — the caller's own draft only, like delete.
            draft = await db["content_drafts"].find_one({"id": draft_id, "user_id": user_id})
            if not draft:
                return UriResponse.error_response("Draft not found")

            # Get original request
            request = await db["content_requests"].find_one({"id": draft["request_id"]})
            if not request:
                return UriResponse.error_response("Original request not found")

            # ==================== PRD 3.2: Retry Rules ====================
            # Track current retry count
            current_retry_count = request.get("retry_count", 0)
            new_retry_count = current_retry_count + 1

            # PRD 3.2: First retry FREE, Second retry = 1 credit
            if new_retry_count >= 2:
                # Check if user has sufficient credits
                has_credits = await credit_service.check_sufficient_credits(user_id)
                if not has_credits:
                    return UriResponse.error_response(
                        "Insufficient credits for retry. This action requires 1 credit.",
                        response_code=402
                    )

            # Generate new content with feedback
            enhanced_seed = request["seed_content"]
            if regeneration_feedback:
                enhanced_seed += f"\n\nUser feedback: {regeneration_feedback}"

            # "Regenerate" used to write with no brand at all: the new version
            # ignored the whole Playbook (voice, banned words, language, CTA).
            from .brand_profile_service import BrandProfileService
            _brand_id = draft.get("brand_id") or request.get("brand_id")
            _profile = await db["brand_profiles"].find_one(
                {"brand_id": _brand_id} if _brand_id else {"user_id": user_id}
            )
            brand_context = None
            if _profile:
                _profile.pop("_id", None)
                brand_context = BrandProfileService.to_brand_context(_profile)

            result = await ContentGenerationService._generate_platform_content(
                platform=draft["platform"],
                seed_content=enhanced_seed,
                request_id=draft["request_id"],
                user_id=user_id,
                brand_context=brand_context,
            )

            if result.get('status'):
                new_draft_data = result['responseData']

                # ==================== PRD 7.2: Credit Deduction ====================
                # Deduct credit if this is second+ retry
                if new_retry_count >= 2:
                    is_trial_user = await trial_service.has_active_trial(user_id)
                    if is_trial_user:
                        await trial_service.deduct_trial_credit(
                            user_id=user_id,
                            campaign_id=draft["request_id"],
                            reason="retry",
                        )
                    else:
                        await credit_service.deduct_credit(
                            user_id=user_id,
                            campaign_id=draft["request_id"],
                            reason="retry",
                            retry_count=new_retry_count
                        )
                    print(f"✅ Deducted 1 credit for retry #{new_retry_count} - user {user_id}")

                # Update request retry count (PRD 9: Campaign Tracking)
                await db["content_requests"].update_one(
                    {"id": draft["request_id"]},
                    {
                        "$set": {
                            "retry_count": new_retry_count,
                            "updated_at": datetime.utcnow()
                        },
                        "$inc": {"credits_used": 1 if new_retry_count >= 2 else 0}
                    }
                )

                # Rewrite the draft in place. This used to insert a second draft for
                # the same request and platform, which the unique index on
                # (request_id, platform) rejects — so every regenerate failed with a
                # duplicate-key error (after the retry had already been counted), and
                # the copy it tried to make had no owner and no image anyway.
                new_draft_id = draft_id
                now = datetime.utcnow()
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {
                        "$set": {
                            "content": new_draft_data["content"],
                            "original_content": new_draft_data["content"],
                            "hashtags": new_draft_data.get("hashtags", []),
                            "regeneration_feedback": regeneration_feedback,
                            "retry_number": new_retry_count,  # Track which retry this is
                            "credit_charged": new_retry_count >= 2,  # Was credit charged?
                            "ai_metadata": new_draft_data.get("ai_metadata"),
                            "updated_at": now,
                        },
                        # The version it replaced stays recoverable.
                        "$push": {"previous_versions": {
                            "content": draft.get("content"),
                            "hashtags": draft.get("hashtags", []),
                            "replaced_at": now,
                        }},
                    },
                )

                return UriResponse.get_single_data_response("content_regeneration", {
                    "original_draft_id": draft_id,
                    "new_draft_id": new_draft_id,
                    "new_content": new_draft_data["content"],
                    "hashtags": new_draft_data.get("hashtags", []),
                    "regeneration_feedback": regeneration_feedback,
                    "retry_number": new_retry_count,
                    "credit_charged": new_retry_count >= 2,
                    "remaining_free_retries": max(0, 1 - current_retry_count),  # PRD: First retry free
                    "regenerated_at": datetime.utcnow().isoformat()
                })
            else:
                return result

        except Exception as e:
            return UriResponse.error_response(f"Content regeneration failed: {str(e)}")
    
    @staticmethod
    async def schedule_content(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_ids: List[str],
        scheduled_datetime: datetime,
        timezone: str = "UTC"
    ) -> Dict[str, Any]:
        """
        Schedule approved content for future publishing
        """
        try:
            scheduled_drafts = []
            
            for draft_id in draft_ids:
                await db["content_drafts"].update_one(
                    {"id": draft_id, "status": {"$in": ["approved", "ready_to_publish"]}},
                    {
                        "$set": {
                            "status": "scheduled",
                            "scheduled_date": scheduled_datetime,
                            "timezone": timezone,
                            "updated_at": datetime.utcnow()
                        }
                    }
                )
                
                scheduled_drafts.append(draft_id)
            
            return UriResponse.get_single_data_response("content_scheduling", {
                "scheduled_drafts": scheduled_drafts,
                "scheduled_datetime": scheduled_datetime.isoformat(),
                "timezone": timezone,
                "scheduled_at": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            return UriResponse.error_response(f"Content scheduling failed: {str(e)}")
    
    @staticmethod
    async def publish_scheduled_content(db: AsyncIOMotorDatabase):
        """
        Background task to publish scheduled content
        This should be run periodically (e.g., every 5 minutes)
        """
        try:
            import os as _os
            _app_env = _os.getenv("APP_ENV", "production")
            # Staging uses "staging_scheduled" so the prod cron (which only queries
            # "scheduled") cannot claim and double-publish staging drafts.
            _sched_status = "staging_scheduled" if _app_env != "production" else "scheduled"

            current_time = datetime.utcnow()
            print(f"📅 publish_scheduled_content | starting at {current_time.isoformat()} env={_app_env} status={_sched_status}")
            from app.agents.social_media_manager.services import publish_dispatch as _pd

            # Clean up stale "publishing" drafts (process crashed mid-publish > 10 min ago).
            # Do NOT clear platform_post_id here — if a post_id was already stored (step 1
            # of the two-phase status update below), the condition-3/4 "confirmed live"
            # detection will mark the draft published on the next cron run without re-publishing.
            stale_cutoff = current_time - timedelta(minutes=10)
            stale_result = await db["content_drafts"].update_many(
                {"status": "publishing", "updated_at": {"$lte": stale_cutoff}},
                {"$set": {"status": _sched_status, "updated_at": current_time}},
            )
            if stale_result.modified_count:
                print(f"⚠️ Stale cleanup: reset {stale_result.modified_count} 'publishing' drafts back to 'scheduled' (were stuck >10min)")

            # Expire overdue scheduled drafts (>24h past due, not yet sent).
            earliest_allowed = current_time - timedelta(hours=24)
            expired_result = await db["content_drafts"].update_many(
                {
                    "status": _sched_status,
                    "scheduled_date": {"$lte": earliest_allowed},
                    "platform_post_id": None,
                    # A draft deliberately waiting for a rate limit to clear is not overdue until
                    # that wait itself is more than a day old.
                    **_pd.not_waiting(earliest_allowed),
                },
                {"$set": {"status": "publish_failed", "error_message": "Scheduled time expired (>24h overdue). Re-schedule to publish.", "updated_at": current_time}},
            )
            if expired_result.modified_count:
                print(f"⏰ Expired {expired_result.modified_count} overdue scheduled drafts (>24h past due)")

            # Stop retrying platform_post_id drafts that have been stuck in publish_failed for >48h.
            stale_poll_cutoff = current_time - timedelta(hours=48)
            stale_poll_result = await db["content_drafts"].update_many(
                {
                    "status": "publish_failed",
                    "platform_post_id": {"$exists": True, "$ne": None},
                    "updated_at": {"$lte": stale_poll_cutoff},
                },
                {"$set": {"status": "publish_failed", "platform_post_id": None, "error_message": "Post confirmation timed out (>48h). Re-schedule to retry.", "updated_at": current_time}},
            )
            if stale_poll_result.modified_count:
                print(f"🧹 Cleared {stale_poll_result.modified_count} stale publish_failed drafts (>48h unconfirmed)")

            # Retry publish_failed drafts that never made it to a platform (no platform_post_id)
            # and are overdue. Only targets BSON Date fields so $lte works. Cap at 3 retries
            # to avoid infinite loops for permanent content errors (e.g. carousel with no images).
            retry_result = await db["content_drafts"].update_many(
                {
                    "status": "publish_failed",
                    "platform_post_id": None,
                    "scheduled_date": {"$lte": current_time, "$type": "date"},
                    "publish_retry_count": {"$not": {"$gte": 3}},
                    # A daily / app-wide cap is not retried: it only burns the same limit again.
                    "publish_error_kind": {"$nin": list(_pd.CAP_KINDS)},
                },
                {
                    "$set": {"status": _sched_status, "updated_at": current_time},
                    "$inc": {"publish_retry_count": 1},
                },
            )
            if retry_result.modified_count:
                print(f"🔄 Reset {retry_result.modified_count} publish_failed draft(s) to {_sched_status} for retry")

            # Posts handed to Outstand before it was retired: nothing can confirm them any more.
            # Once an hour past their time they are marked failed, with a message telling the owner
            # to check the page and reschedule if needed (a Facebook direct id, digits_digits, is
            # a post that exists on the Page and is still marked published below).
            outstand_era = await db["content_drafts"].update_many(
                {
                    "status": {"$in": [_sched_status, "confirming"]},
                    "platform_post_id": {"$exists": True, "$nin": [None, ""], "$not": {"$regex": r"^\d+_\d+$"}},
                    "scheduled_date": {"$lte": current_time - timedelta(hours=1)},
                },
                {"$set": {
                    "status": "publish_failed",
                    "platform_post_id": None,
                    "error_message": "We couldn't confirm this post went out. Check your page; if it isn't there, schedule it again.",
                    "publish_error_kind": "unconfirmed",
                    "updated_at": current_time,
                }},
            )
            if outstand_era.modified_count:
                print(f"🧹 {outstand_era.modified_count} post(s) handed to Outstand earlier marked unconfirmed (Outstand is retired)")

            scheduled_content = await db["content_drafts"].find({
                "$or": [
                    # Drafts due for publishing (no platform_post_id = not yet sent anywhere)
                    {"status": _sched_status, "scheduled_date": {"$lte": current_time}, "platform_post_id": {"$exists": False},
                     **_pd.not_waiting(current_time)},
                    # Drafts due for publishing with a null platform_post_id
                    {"status": _sched_status, "scheduled_date": {"$lte": current_time}, "platform_post_id": None,
                     **_pd.not_waiting(current_time)},
                    # A Facebook post already on the Page (direct id): marked published below
                    {"status": _sched_status, "platform_post_id": {"$regex": r"^\d+_\d+$"}},
                ]
            }).to_list(length=100)

            print(f"📋 Found {len(scheduled_content)} candidate draft(s) | ids={[d.get('id') for d in scheduled_content]} statuses={[d.get('status') for d in scheduled_content]} platforms={[d.get('platform') for d in scheduled_content]} user_ids={[str(d.get('user_id'))[:8] for d in scheduled_content]} sched_dates={[str(d.get('scheduled_date'))[:16] for d in scheduled_content]} post_ids={[d.get('platform_post_id') for d in scheduled_content]}")

            if not scheduled_content:
                return {"message": "No scheduled content to publish", "published": 0}

            # REMOVED (was here from 2026-05-15 to 2026-09-28, commit 46d4424): a
            # "dedup" step that kept only the single newest-CREATED draft per
            # (user_id, platform) and silently cancelled every other one to
            # status="replaced" on every cron tick — with no scheduled_date
            # awareness at all. That's indistinguishable from having two or more
            # DIFFERENT posts legitimately scheduled for different days on the
            # same platform (the normal shape of any real content calendar):
            # every tick it killed all but the most-recently-created one,
            # regardless of which was actually due next. Confirmed live-reported:
            # a user's scheduled posts vanished from the Scheduled tab (the
            # frontend filters "replaced" out of every active view) and never
            # reached Facebook — with zero notification, because "replaced" was
            # never surfaced anywhere as an error.
            #
            # It was added to fix a real bug ("Facebook was posting 8 times"),
            # but that was root-caused to duplicate DRAFT CREATION (a double-
            # submit producing several separate draft docs for what the user
            # intended as one post) — this blanket keep-only-the-newest rule
            # never actually addressed that at the source, it just usually
            # happened to hide most of the resulting duplicates as a side
            # effect, at the cost of also hiding every legitimately distinct
            # future post. The two safeguards that DO correctly prevent
            # double-publishing are both still here and don't have this
            # failure mode:
            #   1. approve_content cancels only genuinely in-flight drafts
            #      ("publishing"/"ready_to_publish") for the same platform at
            #      the moment a new one is explicitly scheduled — never touches
            #      other cleanly-"scheduled" future posts.
            #   2. The atomic find_one_and_update claim below (status=
            #      _sched_status -> "publishing") ensures only one cron run can
            #      ever pick up a given draft, so concurrent ticks can't
            #      double-publish the same draft.
            # A real fix for duplicate draft creation belongs at that source
            # (idempotency on the create/approve action), not here.

            print(f"✅ After dedup: {len(scheduled_content)} draft(s) to process | ids={[d.get('id') for d in scheduled_content]}")

            published_count = 0
            errors = []

            for draft in scheduled_content:
                try:
                    # If this draft was already submitted (has platform_post_id), determine
                    # how to handle it based on the ID format:
                    # - Facebook direct post IDs look like "1234567_9876543" (digits_digits)
                    # - any other id was handed to Outstand before it was retired
                    existing_post_id = draft.get("platform_post_id")
                    if existing_post_id:
                        import re as _re
                        _is_fb_direct_id = bool(_re.match(r'^\d+_\d+$', str(existing_post_id)))
                        if _is_fb_direct_id:
                            # This is a Facebook Graph API post ID stored as platform_post_id.
                            # The post already exists on Facebook — mark as published so this
                            # draft stops cycling through the cron.
                            await db["content_drafts"].update_one(
                                {"id": draft["id"]},
                                {"$set": {
                                    "status": "published",
                                    "published_date": datetime.utcnow(),
                                    "updated_at": datetime.utcnow(),
                                    "error_message": None,
                                }},
                            )
                            published_count += 1
                            print(f"✅ Facebook direct post confirmed live | draft_id={draft['id']} fb_post_id={existing_post_id}")
                        # Any other post id was handed to Outstand (retired) and is settled by the
                        # "marked unconfirmed" step above once it is overdue; until then it waits.
                        continue  # Never re-publish a draft that already has a post_id

                    # Atomically claim this draft before publishing to prevent concurrent cron
                    # runs from double-publishing the same post.  If another worker already
                    # claimed it (status changed to "publishing"), skip and move on.
                    print(f"🔒 Claiming draft {draft.get('id')} | current status={draft.get('status')} platform={draft.get('platform')} user_id={draft.get('user_id')} sched={draft.get('scheduled_date')}")
                    claimed = await db["content_drafts"].find_one_and_update(
                        {"id": draft["id"], "status": _sched_status},
                        {"$set": {"status": "publishing", "updated_at": datetime.utcnow()}},
                    )
                    if not claimed:
                        print(f"⏭️ Draft {draft['id']} already claimed by another cron run — skipping")
                        continue
                    print(f"✅ Claimed draft {draft.get('id')} — proceeding to publish")

                    # Use user_id stored directly on the draft
                    draft_user_id = draft.get("user_id")
                    if not draft_user_id:
                        # Fallback: derive from linked content request
                        request_doc = await db["content_requests"].find_one({"id": draft.get("request_id")})
                        draft_user_id = request_doc["user_id"] if request_doc else None

                    if not draft_user_id:
                        errors.append({"draft_id": draft["id"], "error": "Cannot determine user_id for draft"})
                        continue

                    # Use the account's direct connection (one with publishing credentials).
                    # Accounts still marked as connected through Outstand are never used.
                    _platform_key = draft["platform"]
                    if _platform_key == "linkedin":
                        # Prefer direct connections with an access token
                        connection = await db["social_connections"].find_one({
                            "user_id": draft_user_id,
                            "platform": _platform_key,
                            "connection_status": "active",
                            "linkedin_access_token": {"$exists": True, "$ne": None},
                        })
                        print(f"🔍 LinkedIn token-first lookup | user={draft_user_id} found={bool(connection)} connected_via={connection.get('connected_via') if connection else 'N/A'} has_token={bool(connection.get('linkedin_access_token')) if connection else False} has_urn={bool(connection.get('person_urn') or connection.get('active_author_urn')) if connection else False}")
                        if not connection:
                            connection = await db["social_connections"].find_one({
                                "user_id": draft_user_id,
                                "platform": _platform_key,
                                "connection_status": "active",
                                "connected_via": {"$ne": "outstand"},
                            })
                            print(f"🔍 LinkedIn fallback lookup | found={bool(connection)} connected_via={connection.get('connected_via') if connection else 'N/A'} has_token={bool(connection.get('linkedin_access_token')) if connection else False} has_urn={bool(connection.get('person_urn') or connection.get('active_author_urn')) if connection else False}")
                    else:
                        connection = None
                        if _platform_key == "facebook":
                            connection = await _pd.preferred_direct_facebook(db, draft_user_id, draft.get("brand_id"))
                        elif _platform_key == "tiktok":
                            # The same account the Post to TikTok screen and Publish Now use.
                            from app.agents.social_media_manager.services.tiktok_direct_service import resolve_tiktok_connection
                            _, connection = await resolve_tiktok_connection(db, draft_user_id)
                        if not connection:
                            connections_cursor = db["social_connections"].find({
                                "user_id": draft_user_id,
                                "platform": _platform_key,
                                "connection_status": "active",
                                "connected_via": {"$ne": "outstand"},  # Outstand is retired
                            }).sort("created_at", -1).limit(1)
                            conn_list = await connections_cursor.to_list(length=1)
                            connection = conn_list[0] if conn_list else None

                    if not connection:
                        err_msg = f"No active {draft['platform']} account connected. Reconnect in Settings → Connected Accounts."
                        errors.append({"draft_id": draft["id"], "error": err_msg})
                        await db["content_drafts"].update_one(
                            {"id": draft["id"]},
                            {"$set": {"status": "publish_failed", "error_message": err_msg, "updated_at": datetime.utcnow()}},
                        )
                        print(f"❌ No connection for draft {draft['id']} ({draft['platform']}) — marked publish_failed")
                        continue

                    print(f"🕐 Publishing scheduled post | draft_id={draft['id']} platform={draft['platform']} connected_via={connection.get('connected_via')} user_id={draft_user_id}")
                    async def _send():
                        return await ApprovalWorkflowService._publish_to_platform(
                            platform=draft["platform"],
                            draft=draft,
                            connection=connection,
                            scheduled_datetime=None,  # We publish immediately when the time arrives
                            db=db,
                        )

                    # One post at a time per account, spaced out — and never wait inside the tick
                    # (it is bounded to 300s): if the account is busy or rate limited, put the draft
                    # back with the time to try again and move on to the next one.
                    sent = await _pd.dispatch(db, draft["platform"], connection, _send, wait_max_s=0.0)
                    if sent.deferred:
                        await db["content_drafts"].update_one(
                            {"id": draft["id"], "status": "publishing"},
                            {"$set": _pd.deferral_fields(draft, sent.resume_at, sent.info, sent.message)},
                        )
                        print(f"⏳ Deferred scheduled draft {draft.get('id')} ({draft.get('platform')}): {sent.message}")
                        continue
                    publish_result, limit_info = sent.result, sent.info
                    if publish_result is None:
                        publish_result = {"success": False, "error": f"_publish_to_platform returned None for platform={draft['platform']} connected_via={connection.get('connected_via')} — unhandled publish path"}
                    print(f"📊 Scheduled publish result | draft_id={draft['id']}: {publish_result}")

                    conn_filter = (
                        {"id": connection["id"]} if connection.get("id")
                        else {"user_id": draft_user_id, "platform": draft["platform"], "connected_via": connection.get("connected_via")}
                    )
                    print(f"📊 Publish result for draft {draft.get('id')}: success={publish_result.get('success')} post_id={publish_result.get('post_id')} error={publish_result.get('error')}")
                    from app.agents.social_media_manager.services import publish_confirmation as _pc
                    if publish_result.get("success"):
                        _post_id = publish_result.get("post_id")
                        # Phase 1: persist post_id while draft is still "publishing".
                        # If phase 2 hangs and the stale cleanup later resets status back
                        # to "scheduled", the post_id survives and the confirmed-live
                        # detection on the next cron run marks it published — no re-publish.
                        if _post_id:
                            await db["content_drafts"].update_one(
                                {"id": draft["id"]},
                                {"$set": {"platform_post_id": _post_id, "updated_at": datetime.utcnow()}},
                            )
                        # Phase 2: mark as published
                        update_res = await db["content_drafts"].update_one(
                            {"id": draft["id"]},
                            {"$set": {
                                "status": "published",
                                "published_date": datetime.utcnow(),
                                "platform_post_id": _post_id,
                                "publish_response": publish_result.get("raw_response"),
                                "updated_at": datetime.utcnow(),
                                "error_message": None,
                            },
                             "$unset": _pd.CLEAR_WAIT},
                        )
                        print(f"💾 Status→published update | draft={draft.get('id')} matched={update_res.matched_count} modified={update_res.modified_count}")
                        await db["social_connections"].update_one(conn_filter, {"$inc": {"total_posts_published": 1}})
                        published_count += 1

                        # Notification PRD 4.3: Content posted (scheduled publish)
                        try:
                            from app.services.NotificationService import notification_service
                            await notification_service.notify_content_posted(
                                user_id=draft_user_id,
                                platform=draft.get("platform", ""),
                                content_preview=(draft.get("content") or "")[:120],
                                campaign_id=draft.get("campaign_id", ""),
                                brand_id=draft.get("brand_id"),
                            )
                        except Exception as e:
                            print(f"⚠️ Content posted notification failed: {e}")
                    else:
                        _msg = await _pc.failed_on_submit(db, draft, connection, publish_result.get("error") or "", limit_info)
                        await db["content_drafts"].update_one(
                            {"id": draft["id"]},
                            {"$set": {
                                "status": "publish_failed",
                                "error_message": _msg,
                                "network_error": (publish_result.get("error") or "")[:1000] or None,
                                "updated_at": datetime.utcnow(),
                                **_pd.cap_fields(limit_info),
                            }},
                        )
                        await db["social_connections"].update_one(conn_filter, {"$inc": {"total_publish_errors": 1}})
                        errors.append({"draft_id": draft["id"], "error": publish_result.get("error")})

                except Exception as e:
                    draft_id_for_error = draft.get("id", "unknown")
                    errors.append({"draft_id": draft_id_for_error, "error": str(e)})
                    print(f"❌ Exception during scheduled publish for draft_id={draft_id_for_error}: {e}")
                    # Reset "publishing" back to "publish_failed" so the draft doesn't get stuck
                    if draft_id_for_error != "unknown":
                        try:
                            await db["content_drafts"].update_one(
                                {"id": draft_id_for_error, "status": "publishing"},
                                {"$set": {"status": "publish_failed", "error_message": str(e), "updated_at": datetime.utcnow()}},
                            )
                        except Exception:
                            pass

            return {
                "published_count": published_count,
                "errors": errors,
                "processed_at": datetime.utcnow().isoformat()
            }

        except Exception as e:
            # Live-reported bug (on prod, ported here): this used to return
            # {"error": ...} (singular) with no print at all — a crash here (e.g. the
            # created_at TypeError above, before it was fixed) silently aborted the
            # ENTIRE candidate batch every single tick it recurred, with zero trace in
            # the logs, because the scheduler wrapper
            # (notification_scheduler.py's _job_publish_scheduled_content) only ever
            # checks the plural key "errors". Print unconditionally here so a future
            # batch-level crash is never invisible again, and return the same "errors"
            # shape the wrapper actually reads.
            print(f"❌ publish_scheduled_content crashed before processing any candidate: {e}")
            import traceback
            traceback.print_exc()
            return {"published_count": 0, "errors": [{"draft_id": "batch", "error": str(e)}]}

    @staticmethod
    async def _trigger_immediate_publishing(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_ids: List[str],
        scheduled_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Publish approved drafts now, through each account's direct connection.
        """
        results = {}
        from app.agents.social_media_manager.services import publish_dispatch as _pd
        from app.agents.social_media_manager.services import rate_limit as _rl

        for draft_id in draft_ids:
            try:
                draft = await db["content_drafts"].find_one({"id": draft_id})
                if not draft:
                    results[draft_id] = {"success": False, "error": "Draft not found"}
                    continue

                platform = draft["platform"]
                print(f"🚀 Immediate publish | draft_id={draft_id} platform={platform} user_id={user_id}")

                connection = None
                # TikTok: prefer a direct-OAuth connection (FILE_UPLOAD, bypasses
                # Outstand) over the generic lookup below — that query doesn't
                # filter by connected_via, so it would otherwise pick whichever
                # connection happens to be most recently created rather than
                # reliably preferring direct.
                if platform == "tiktok":
                    connection = await db["social_connections"].find_one({
                        "user_id": user_id,
                        "platform": "tiktok",
                        "connected_via": "tiktok_direct_oauth",
                        "connection_status": "active",
                    })
                    if connection:
                        print(f"🔗 Preferring direct TikTok connection open_id={connection.get('open_id')}")

                # Facebook: the brand's Page connected directly with Meta.
                if platform == "facebook":
                    connection = await _pd.preferred_direct_facebook(db, user_id, draft.get("brand_id"))
                    if connection:
                        print(f"🔗 Preferring direct Facebook connection page_id={connection.get('page_id')}")

                if not connection:
                    connections_cursor = db["social_connections"].find({
                        "user_id": user_id,
                        "platform": platform,
                        "connection_status": "active",
                        "connected_via": {"$ne": "outstand"},  # Outstand is retired
                    }).sort("created_at", -1).limit(1)
                    connection = await connections_cursor.to_list(length=1)
                    connection = connection[0] if connection else None

                if not connection:
                    print(f"❌ No active {platform} connection found for user_id={user_id}")
                    results[draft_id] = {
                        "success": False,
                        "error": f"No {platform.capitalize()} account connected. Connect it on Connected Accounts first.",
                    }
                    continue

                print(f"🔗 Using connection: connected_via={connection.get('connected_via')}")

                async def _send():
                    return await ApprovalWorkflowService._publish_to_platform(
                        platform=platform,
                        draft=draft,
                        connection=connection,
                        scheduled_datetime=scheduled_datetime,
                        db=db,
                    )

                limit_info = None
                if scheduled_datetime is not None:
                    # A post with a future send time: sent as is (the scheduler gates the real send).
                    publish_result = await _send()
                else:
                    # One post at a time per account, spaced out. If the account is busy or rate
                    # limited, wait only a few seconds, then answer "queued" and let the scheduler
                    # post it — not a failure, and not counted as one.
                    sent = await _pd.dispatch(db, platform, connection, _send,
                                              wait_max_s=_rl.get_settings().immediate_wait_max_s)
                    if sent.deferred:
                        await db["content_drafts"].update_one(
                            {"id": draft_id},
                            {"$set": _pd.deferral_fields(draft, sent.resume_at, sent.info, sent.message),
                             "$unset": {"last_dispatch_warning": ""}},
                        )
                        print(f"⏳ Deferred draft_id={draft_id}: {sent.message}")
                        results[draft_id] = _pd.queued_result(sent)
                        continue
                    publish_result, limit_info = sent.result, sent.info
                print(f"📊 Publish result for draft_id={draft_id}: {publish_result}")

                conn_filter = (
                    {"id": connection["id"]} if connection.get("id")
                    else {"user_id": user_id, "platform": platform, "connected_via": connection.get("connected_via")}
                )
                is_scheduled = scheduled_datetime is not None
                from app.agents.social_media_manager.services import publish_confirmation as _pc
                if publish_result.get("success"):
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {
                            "$set": {
                                "status": "scheduled" if is_scheduled else "published",
                                "published_date": None if is_scheduled else datetime.utcnow(),
                                "scheduled_date": scheduled_datetime if is_scheduled else None,
                                "platform_post_id": publish_result.get("post_id"),
                                "publish_response": publish_result.get("raw_response"),
                                "updated_at": datetime.utcnow(),
                            },
                            "$unset": {"last_dispatch_warning": "", **_pd.CLEAR_WAIT},
                        },
                    )
                    await db["social_connections"].update_one(conn_filter, {"$inc": {"total_posts_published": 1}})

                    # Notification PRD 4.3: Content posted (immediate publish)
                    if not is_scheduled:
                        try:
                            from app.services.NotificationService import notification_service
                            await notification_service.notify_content_posted(
                                user_id=user_id,
                                platform=platform,
                                content_preview=(draft.get("content") or "")[:120],
                                campaign_id=draft.get("campaign_id", ""),
                                brand_id=draft.get("brand_id"),
                            )
                        except Exception as e:
                            print(f"⚠️ Content posted notification failed: {e}")

                elif is_scheduled:
                    # When scheduling, the draft was already marked status=scheduled in
                    # approve_content. Keep that status — don't overwrite to publish_failed.
                    # The cron job will attempt the actual publish at the right time.
                    # last_dispatch_warning is informational only (surfaced to the user at
                    # schedule time via approve_content's warnings[] — see there for why this
                    # is kept separate from error_message/publish_failed) and is cleared the
                    # next time a dispatch attempt succeeds, so a stale warning from an
                    # earlier reschedule never lingers on a post that's since gone out fine.
                    print(f"⚠️ Scheduled dispatch failed for draft_id={draft_id}, keeping status=scheduled: {publish_result.get('error')}")
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {
                            "status": "scheduled",
                            "scheduled_date": scheduled_datetime,
                            "last_dispatch_warning": _pc.specific_message(platform, publish_result.get("error") or "")
                            or publish_result.get("error"),
                            "updated_at": datetime.utcnow(),
                        }},
                    )
                else:
                    _msg = await _pc.failed_on_submit(db, draft, connection, publish_result.get("error") or "", limit_info)
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {
                            "status": "publish_failed",
                            "error_message": _msg,
                            "network_error": (publish_result.get("error") or "")[:1000] or None,
                            "updated_at": datetime.utcnow(),
                            **_pd.cap_fields(limit_info),
                        }},
                    )
                    await db["social_connections"].update_one(conn_filter, {"$inc": {"total_publish_errors": 1}})
                    publish_result["error"] = _msg

                results[draft_id] = publish_result

            except Exception as e:
                print(f"❌ Publish exception for draft_id={draft_id}: {e}")
                results[draft_id] = {"success": False, "error": str(e)}

        return results

    @staticmethod
    def _facebook_result(success: bool, post_id, response) -> Dict[str, Any]:
        """The result of a direct Facebook feed/carousel publish. On failure it now carries what Meta
        said (`error`) and, for a throttle, the typed `rate_limit` — before, a refused post came back
        with no error at all, so a Facebook rate limit (or an expired token) was invisible."""
        result: Dict[str, Any] = {"success": success, "post_id": post_id, "raw_response": response}
        if not success and isinstance(response, dict):
            meta = response.get("meta_error") or {}
            if meta.get("message"):
                result["error"] = f"Facebook publish failed: {meta['message']}"
            if response.get("rate_limit") is not None:
                result["rate_limit"] = response["rate_limit"]
        return result

    @staticmethod
    async def _upload_base64_to_imgbb(data_url: str) -> Optional[str]:
        """
        Upload a base64 data URL image to Cloudinary and return a permanent public URL.
        Used everywhere we need a public HTTPS URL for Instagram or Facebook.
        Returns the Cloudinary secure_url string or None on failure.
        """
        try:
            from app.utils.s3_upload import upload_base64 as _cld_upload
            url = await _cld_upload(data_url, folder="uri-social/drafts")
            print(f"📸 Image uploaded to Cloudinary: {url}")
            return url
        except Exception as e:
            print(f"⚠️ Cloudinary upload exception: {e}")
            return None

    @staticmethod
    def _resolve_image_url(url: str) -> str:
        """
        Convert a relative /static/images/... path to a full public URL.
        Absolute URLs (http/https) and data: URLs are returned unchanged.
        """
        if not url or url.startswith("http") or url.startswith("data:"):
            return url
        if url.startswith("/"):
            base = (settings.PUBLIC_API_URL or "").rstrip("/")
            return f"{base}{url}"
        return url

    @staticmethod
    async def _normalize_image_for_tiktok(url: str) -> str:
        """
        Make an image compliant with TikTok's Content Posting API photo
        requirements before it's sent — confirmed from TikTok's own Media
        Transfer Guide: JPEG or WebP only (no PNG), max 1080p, max 20MB per
        image. Live-confirmed 2026-10-10: a real carousel post failed with
        "picture_size_check_failed" — these are exactly the checks that
        error covers, and nothing in the upload pipeline validated or
        resized a user's photo against them before this.

        Downloads, re-encodes to JPEG if needed, resizes so neither
        dimension exceeds 1080px (preserving aspect ratio), and re-uploads.
        Returns the original url unchanged if it already complies — avoids
        a pointless re-upload/quality loss on images that were already fine.
        On any failure, logs and returns the original url so a normalization
        bug never blocks a publish that might otherwise have succeeded.
        """
        try:
            import io
            import httpx as _httpx
            from PIL import Image as _Image

            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            raw_bytes = resp.content

            img = _Image.open(io.BytesIO(raw_bytes))
            fmt = (img.format or "").upper()
            needs_format_fix = fmt not in ("JPEG", "WEBP")
            needs_resize = max(img.size) > 1080
            needs_size_fix = len(raw_bytes) > 20 * 1024 * 1024

            if not (needs_format_fix or needs_resize or needs_size_fix):
                return url

            if needs_resize:
                scale = 1080 / max(img.size)
                new_size = (round(img.width * scale), round(img.height * scale))
                img = img.resize(new_size, _Image.LANCZOS)
                print(f"🔧 TikTok image resized {url} -> {new_size}")

            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            jpeg_bytes = buf.getvalue()

            if len(jpeg_bytes) > 20 * 1024 * 1024:
                # Re-encode at a lower quality rather than give up — a 1080px-
                # capped JPEG this large would be unusual, but stay inside
                # TikTok's limit if it happens.
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=70)
                jpeg_bytes = buf.getvalue()

            import base64 as _b64
            data_url = f"data:image/jpeg;base64,{_b64.b64encode(jpeg_bytes).decode()}"
            normalized_url = await ApprovalWorkflowService._upload_base64_to_imgbb(data_url)
            if normalized_url:
                print(f"🔧 TikTok image normalized: {url} -> {normalized_url} "
                      f"(format_fix={needs_format_fix}, resize={needs_resize}, size_fix={needs_size_fix})")
                return normalized_url
            print(f"⚠️ TikTok image normalization: re-upload failed, using original url: {url}")
            return url
        except Exception as e:
            print(f"⚠️ TikTok image normalization failed for {url}, using original: {e}")
            return url

    @staticmethod
    async def _upload_base64_image_to_facebook(
        page_id: str,
        page_token: str,
        data_url: str,
    ) -> Optional[str]:
        """
        Upload a base64 data URL image to Facebook as binary multipart data.
        Returns the Facebook media ID (media_fbid) or None on failure.
        """
        try:
            match = re.match(r'data:([^;]+);base64,(.+)', data_url, re.DOTALL)
            if not match:
                print("⚠️ Image upload: invalid data URL format")
                return None

            mime_type = match.group(1)
            image_bytes = base64.b64decode(match.group(2))
            ext = mime_type.split('/')[-1]

            url = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/photos"

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    url,
                    data={"published": "false", "access_token": page_token},
                    files={"source": (f"image.{ext}", image_bytes, mime_type)},
                ),
            )
            resp_json = response.json()
            media_id = resp_json.get("id")
            if media_id:
                print(f"📸 Image uploaded to Facebook: media_fbid={media_id}")
            else:
                print(f"⚠️ Facebook image upload failed: {resp_json}")
            return media_id
        except Exception as e:
            print(f"⚠️ Failed to upload base64 image to Facebook: {e}")
            return None

    @staticmethod
    async def _publish_facebook_story(
        page_id: str,
        page_token: str,
        story_image_url: str,
    ) -> Dict[str, Any]:
        """Upload an image as an unpublished photo then publish it as a Facebook Story."""
        import httpx as _httpx
        from PIL import Image as _PILImg
        import io as _io
        import os as _os

        _raw = None
        if "/static/images/" in story_image_url:
            _fname = story_image_url.split("/static/images/")[-1].split("?")[0]
            _local = f"/app/static/images/{_fname}"
            if _os.path.exists(_local):
                with open(_local, "rb") as _f:
                    _raw = _f.read()
        if _raw is None:
            async with _httpx.AsyncClient(timeout=30) as _c:
                img_resp = await _c.get(story_image_url)
            img_resp.raise_for_status()
            _raw = img_resp.content

        _img = _PILImg.open(_io.BytesIO(_raw)).convert("RGB")
        _buf = _io.BytesIO()
        _img.save(_buf, format="JPEG", quality=92)
        _jpeg_bytes = _buf.getvalue()

        # Step 1: upload as unpublished photo
        async with _httpx.AsyncClient(timeout=60) as _c2:
            upload_resp = await _c2.post(
                f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/photos",
                data={"access_token": page_token, "published": "false"},
                files={"source": ("story.jpg", _jpeg_bytes, "image/jpeg")},
            )
        upload_data = upload_resp.json()
        photo_id = upload_data.get("id")
        if not photo_id:
            error_msg = (upload_data.get("error") or {}).get("message", str(upload_data))
            print(f"❌ FB story photo upload failed: {upload_data}")
            return {"success": False, "error": f"Failed to upload story image to Facebook. (Detail: {error_msg})"}

        print(f"✅ FB story photo uploaded: photo_id={photo_id}")

        # Step 2: publish as story — send photo_ids as JSON body, not form-encoded string
        async with _httpx.AsyncClient(timeout=30) as _c3:
            story_resp = await _c3.post(
                f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/photo_stories",
                json={"access_token": page_token, "photo_ids": [photo_id]},
            )
        story_data = story_resp.json()
        print(f"📋 FB photo_stories response: {story_data}")
        post_id = story_data.get("post_id") or story_data.get("id")
        if post_id:
            print(f"✅ FB story publish success: post_id={post_id}")
            return {"success": True, "post_id": post_id, "raw_response": story_data}
        error_msg = (story_data.get("error") or {}).get("message", str(story_data))
        print(f"❌ FB story publish failed: {story_data}")
        return {"success": False, "error": f"Facebook story publish failed. (Detail: {error_msg})"}

    @staticmethod
    async def _publish_to_platform(
        platform: str,
        draft: Dict[str, Any],
        connection: Dict[str, Any],
        scheduled_datetime: Optional[datetime],
        db=None,
    ) -> Dict[str, Any]:
        """
        Dispatch to the correct platform publisher.
        Returns {"success": bool, "post_id": str|None, "raw_response": dict}.
        """
        # Build content string (append hashtags inline)
        content = (draft.get("content") or "").strip()
        if draft.get("hashtags"):
            tags = " ".join(f"#{t.strip('#')}" for t in draft["hashtags"])
            content = f"{content} {tags}".strip()
        # Every network needs at least 1 character of content
        if not content:
            content = " "

        # ── X direct (OAuth 1.0a) ─────────────────────────────────────────────
        if platform in ("x", "twitter") and connection.get("connected_via") == "x_direct":
            access_token = connection.get("x_oauth_token")
            access_token_secret = connection.get("x_oauth_token_secret")
            if not access_token or not access_token_secret:
                return {"success": False, "error": "X connection is missing OAuth tokens. Please reconnect your account."}
            try:
                svc = XDirectService()
                tweets = draft.get("tweets") if draft.get("is_twitter_thread") and draft.get("tweets") else None
                if tweets and len(tweets) > 1:
                    results = await svc.post_thread(access_token, access_token_secret, tweets)
                    tweet_id = (results[0].get("data") or {}).get("id") if results else None
                else:
                    result = await svc.post_tweet(access_token, access_token_secret, content)
                    tweet_id = (result.get("data") or {}).get("id")
                return {"success": tweet_id is not None, "post_id": tweet_id, "raw_response": result if not tweets else results}
            except Exception as e:
                if "402" in str(e) or "CreditsDepleted" in str(e):
                    print(f"⚠️ X refused the post: Uri's X API credits are used up ({e})")
                    return {"success": False, "error": "X isn't accepting posts from Uri right now. Please try again later."}
                return {"success": False, "error": f"X direct publish failed: {str(e)}"}

        # Instagram connected via direct OAuth always uses the Graph API path below.

        # ── Instagram direct (via Facebook Page Access Token) ────────────────
        # Credential-first: if ig_user_id + page_access_token are present, use the
        # direct Graph API path regardless of how connected_via is stored.
        _ig_cv = connection.get("connected_via")
        _ig_has_creds = bool(connection.get("ig_user_id") and connection.get("page_access_token"))
        _ig_cv_is_direct = _ig_cv in (
            "instagram_direct", "instagram_direct_oauth", "instagram_oauth", "instagram"
        )
        print(f"🔀 _publish_to_platform routing | platform={platform} connected_via={_ig_cv} ig_user_id={connection.get('ig_user_id')} has_page_token={'yes' if connection.get('page_access_token') else 'NO'} has_creds={_ig_has_creds}")
        if platform == "instagram" and (_ig_has_creds or _ig_cv_is_direct):
            from app.agents.social_media_manager.services.instagram_direct_service import InstagramDirectService
            ig_user_id = connection.get("ig_user_id")
            page_token = connection.get("page_access_token")
            page_id = connection.get("page_id")
            print(f"🔗 Instagram connection details | ig_user_id={ig_user_id} page_id={page_id} connected_via={_ig_cv} token_present={'yes' if page_token else 'NO'}")
            if not ig_user_id or not page_token:
                return {"success": False, "error": "Instagram direct connection is missing credentials. Please reconnect Facebook."}

            post_type = draft.get("post_type", "feed")
            # A user-uploaded video on a normal post is stored as video_url with no
            # image_url (upload_user_content). Instagram only takes video as a Reel,
            # and the feed branch below reads image_url only, so these drafts failed
            # with "Instagram requires an image" every time.
            if post_type not in ("carousel", "reel", "story") and not draft.get("image_url") and draft.get("video_url"):
                post_type = "reel"

            if post_type == "carousel":
                slides = draft.get("slides", [])

                # ── VALIDATE ALL CAROUSEL SLIDES HAVE IMAGES ──
                # Unlike Facebook (which handles missing images inline), Instagram requires
                # all slides to have valid public HTTPS URLs before publishing.
                if not slides:
                    return {"success": False, "error": "Carousel has no slides."}

                validated_slides = []
                for i, slide in enumerate(slides):
                    slide_image_url = slide.get("image_url") or ""

                    # Check if image is missing
                    if not slide_image_url:
                        # Check if image generation failed or is still processing
                        if slide.get("image_failed"):
                            return {
                                "success": False,
                                "error": f"Slide {i + 1} image generation failed. Please regenerate the image before publishing."
                            }
                        else:
                            return {
                                "success": False,
                                "error": f"Slide {i + 1} image is still being generated. Please wait a moment and try again."
                            }

                    # Check if image is still a base64 data URL (Cloudinary upload failed)
                    if slide_image_url.startswith("data:"):
                        return {
                            "success": False,
                            "error": f"Slide {i + 1} image failed to upload to cloud storage. Please regenerate the image."
                        }

                    # Check if image is a valid HTTPS URL
                    if not slide_image_url.startswith("https://"):
                        return {
                            "success": False,
                            "error": f"Slide {i + 1} has an invalid image URL. Please regenerate the image."
                        }

                    validated_slides.append(slide)

                if len(validated_slides) < 2:
                    return {
                        "success": False,
                        "error": f"Instagram carousels require at least 2 slides with images. Only {len(validated_slides)} slides have valid images."
                    }

                print(f"📸 Instagram carousel validation passed: {len(validated_slides)} slides with valid image URLs")

                return await InstagramDirectService.publish_carousel(
                    ig_user_id=ig_user_id,
                    page_access_token=page_token,
                    caption=content,
                    slides=validated_slides,
                    page_id=page_id,
                )
            elif post_type == "reel":
                video_url = draft.get("video_url") or ""
                if not video_url:
                    return {"success": False, "error": "Reel draft has no video_url. Re-generate the video before publishing."}
                print(f"🎬 Instagram reel publish | ig_user_id={ig_user_id} video_url={video_url[:80]}")
                return await InstagramDirectService.publish_reel(
                    ig_user_id=ig_user_id,
                    page_access_token=page_token,
                    video_url=video_url,
                    caption=content,
                    page_id=page_id,
                    audio=draft.get("instagram_audio"),
                )
            elif post_type == "story":
                image_url = ApprovalWorkflowService._resolve_image_url(draft.get("image_url") or "")
                if image_url and image_url.startswith("data:"):
                    image_url = await ApprovalWorkflowService._upload_base64_to_imgbb(image_url) or ""
                return await InstagramDirectService.publish_story(
                    ig_user_id=ig_user_id,
                    page_access_token=page_token,
                    image_url=image_url or None,
                    page_id=page_id,
                )
            else:
                image_url = ApprovalWorkflowService._resolve_image_url(draft.get("image_url") or "")
                if image_url and image_url.startswith("data:"):
                    image_url = await ApprovalWorkflowService._upload_base64_to_imgbb(image_url) or ""

                print(f"📱 Instagram feed publish | ig_user_id={ig_user_id} page_id={page_id} token_len={len(page_token) if page_token else 0} raw_image_url={image_url[:120] if image_url else None}")

                return await InstagramDirectService.publish_post(
                    ig_user_id=ig_user_id,
                    page_access_token=page_token,
                    content=content,
                    image_url=image_url or None,
                    scheduled_at=scheduled_datetime.strftime("%Y-%m-%dT%H:%M:%SZ") if scheduled_datetime else None,
                    page_id=page_id,
                )

        # ── LinkedIn direct (OAuth 2.0) ───────────────────────────────────────
        # Also match when connected_via is unset/unexpected but the connection has a token —
        # guards against stale "outstand" entries that got upserted over the direct connection.
        _li_has_token = bool(connection.get("linkedin_access_token") or connection.get("access_token"))
        _li_has_urn = bool(connection.get("person_urn") or connection.get("active_author_urn"))
        if platform == "linkedin":
            print(f"🔗 LinkedIn publish check | connected_via={connection.get('connected_via')} has_token={_li_has_token} has_urn={_li_has_urn} condition={'PASS' if (connection.get('connected_via')=='linkedin_direct' or (_li_has_token and _li_has_urn)) else 'FAIL'}")
        if platform == "linkedin" and (connection.get("connected_via") == "linkedin_direct" or (_li_has_token and _li_has_urn)):
            from app.agents.social_media_manager.services.linkedin_direct_service import LinkedInDirectService
            token = connection.get("linkedin_access_token") or connection.get("access_token")
            urn = connection.get("person_urn") or connection.get("active_author_urn")
            if not token or not urn:
                return {"success": False, "error": "LinkedIn connection is missing OAuth token or author URN. Please reconnect your account."}
            post_type = draft.get("post_type", "feed")
            svc = LinkedInDirectService()
            try:
                # ── LinkedIn carousel ─────────────────────────────────────────
                if post_type == "carousel":
                    slides = draft.get("slides", [])
                    image_urls: List[str] = []
                    for i, slide in enumerate(slides):
                        raw = ApprovalWorkflowService._resolve_image_url(slide.get("image_url") or "")
                        if not raw:
                            print(f"⚠️ LinkedIn carousel slide {i} has no image_url — skipping")
                            continue
                        if raw.startswith("data:"):
                            raw = await ApprovalWorkflowService._upload_base64_to_imgbb(raw) or ""
                        if raw:
                            image_urls.append(raw)
                    if len(image_urls) < 2:
                        return {"success": False, "error": f"LinkedIn carousel needs at least 2 images with URLs; only got {len(image_urls)}."}
                    print(f"📎 LinkedIn carousel: {len(image_urls)} slides for urn={urn}")
                    result = await svc.create_carousel_post(
                        access_token=token,
                        author_urn=urn,
                        text=content,
                        image_urls=image_urls,
                    )
                    post_id = result.get("post_id")
                    print(f"✅ LinkedIn carousel publish: post_id={post_id} slides={result.get('slides')}")
                    return {"success": bool(post_id), "post_id": post_id, "raw_response": result}

                # ── LinkedIn video ─────────────────────────────────────────────
                # video_url (not post_type) is the signal a draft carries video —
                # same convention Instagram/TikTok use elsewhere in this file.
                # Without this branch, a video draft fell into the image/text
                # branch below, which only ever reads image_url, so the video
                # was never uploaded and the post went out as caption-only text.
                video_url = draft.get("video_url") or ""
                if video_url:
                    print(f"🎬 LinkedIn video publish | urn={urn} video_url={video_url[:80]}")
                    result = await svc.create_post(
                        access_token=token,
                        person_urn=urn,
                        text=content,
                        video_url=video_url,
                    )
                    post_id = result.get("post_id")
                    print(f"✅ LinkedIn video publish: post_id={post_id}")
                    return {"success": bool(post_id), "post_id": post_id, "raw_response": result}

                # ── LinkedIn single image / text ──────────────────────────────
                image_url = ApprovalWorkflowService._resolve_image_url(draft.get("image_url") or "")
                if image_url and image_url.startswith("data:"):
                    image_url = await ApprovalWorkflowService._upload_base64_to_imgbb(image_url) or ""
                result = await svc.create_post(
                    access_token=token,
                    person_urn=urn,
                    text=content,
                    image_url=image_url or None,
                )
                post_id = result.get("post_id")
                print(f"✅ LinkedIn direct publish: post_id={post_id}")
                return {"success": bool(post_id), "post_id": post_id, "raw_response": result}
            except Exception as e:
                print(f"❌ LinkedIn direct publish failed: {e}")
                return {"success": False, "error": f"LinkedIn direct publish failed: {str(e)}"}

        # Facebook posts go straight to the Page through its direct connection (the Facebook block
        # further down). Outstand is no longer used for anything.

        # ── Scheduled TikTok image / carousel post: the choices made when scheduling ──
        # (who can view, comments, disclosure) were saved on the draft, and go out with the post
        # whichever way the account is connected.
        if platform == "tiktok" and not draft.get("video_url") and draft.get("tiktok_settings"):
            from app.agents.social_media_manager.services.video_publish_service import VideoPublishService
            return await VideoPublishService.publish_scheduled_tiktok_photo(draft, connection, db)

        # ── TikTok direct (FILE_UPLOAD) ───────────────────────
        if platform == "tiktok" and connection.get("connected_via") == "tiktok_direct_oauth":
            from app.agents.social_media_manager.services.tiktok_direct_service import (
                get_valid_tiktok_access_token,
                publish_tiktok_direct,
                TikTokDirectAPIError,
            )
            video_url = draft.get("video_url") or ""
            if not video_url:
                if draft.get("image_url") or draft.get("slides"):
                    # A photo post scheduled before its posting choices were saved with it.
                    return {"success": False, "error": "This TikTok photo post is missing its posting choices (who can view it, comments, disclosure). Open it and schedule it again."}
                return {"success": False, "error": "TikTok post has no video_url. Re-generate the video before publishing."}
            if db is None:
                return {"success": False, "error": "TikTok direct publish requires a database handle."}
            try:
                access_token = await get_valid_tiktok_access_token(db, connection)
                publish_id, status = await publish_tiktok_direct(access_token, video_url, content)
                print(f"✅ TikTok direct publish: publish_id={publish_id} status={status}")
                return {"success": True, "post_id": publish_id, "raw_response": {"publish_id": publish_id, "status": status}}
            except TikTokDirectAPIError as e:
                print(f"❌ TikTok direct publish failed: {e}")
                failed = {"success": False, "error": f"TikTok direct publish failed: {str(e)}"}
                # A rate limit / cap is carried as typed info so the caller can defer (temporary) or
                # explain (cap) rather than treat it as a plain failure.
                if getattr(e, "rate_limit", None) is not None:
                    failed["rate_limit"] = e.rate_limit
                return failed

        # ── An account still marked as connected through Outstand ────────────────
        # Outstand is no longer used. Such an account can't be posted to until it is connected
        # again directly (Connected Accounts).
        if connection.get("connected_via") == "outstand":
            return {"success": False, "needs_reconnect": True,
                    "error": f"Reconnect your {platform.capitalize()} account on Connected Accounts, then try again."}

        # ── Legacy direct Facebook connection ─────────────────────────────────
        if platform == "facebook":
            page_id = connection.get("page_id")
            page_token = connection.get("page_access_token")

            if not page_id or not page_token:
                return {"success": False, "error": "No valid Facebook credentials on this connection. Reconnect your account."}

            post_type = draft.get("post_type", "feed")

            # ── Facebook carousel: upload each slide image as unpublished photo, then post feed with attached_media ──
            if post_type == "carousel":
                slides = draft.get("slides", [])
                attached_media = []
                for i, slide in enumerate(slides):
                    slide_image_url = slide.get("image_url") or ""
                    if not slide_image_url:
                        print(f"⚠️ FB carousel slide {i} has no image_url — skipping")
                        continue
                    # Upload as unpublished photo to get a media_fbid
                    try:
                        import httpx as _httpx
                        from PIL import Image as _Image
                        import io as _io
                        async with _httpx.AsyncClient(timeout=30) as _c:
                            img_resp = await _c.get(slide_image_url)
                        _img = _Image.open(_io.BytesIO(img_resp.content)).convert("RGB")
                        _buf = _io.BytesIO()
                        _img.save(_buf, format="JPEG", quality=92)
                        _jpeg_bytes = _buf.getvalue()
                        async with _httpx.AsyncClient(timeout=60) as _c2:
                            upload_resp = await _c2.post(
                                f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/photos",
                                data={"access_token": page_token, "published": "false"},
                                files={"source": ("image.jpg", _jpeg_bytes, "image/jpeg")},
                            )
                        photo_id = upload_resp.json().get("id")
                        if photo_id:
                            attached_media.append({"media_fbid": photo_id})
                            print(f"✅ FB carousel slide {i} uploaded: photo_id={photo_id}")
                        else:
                            print(f"⚠️ FB carousel slide {i} upload failed: {upload_resp.json()}")
                    except Exception as slide_err:
                        print(f"⚠️ FB carousel slide {i} upload error: {slide_err}")

                if not attached_media:
                    return {"success": False, "error": "No carousel slide images could be uploaded to Facebook."}

                import calendar
                carousel_payload: Dict[str, Any] = {
                    "message": content,
                    "published": True,
                    "attached_media": attached_media,
                }
                if scheduled_datetime:
                    carousel_payload["published"] = False
                    carousel_payload["scheduled_publish_time"] = int(
                        calendar.timegm(scheduled_datetime.utctimetuple())
                    )
                response = await FacebookService.publish_post(
                    page_id=page_id, payload=carousel_payload, access_token=page_token,
                )
                post_id = None
                success = False
                if isinstance(response, dict):
                    resp_data = response.get("responseData") or {}
                    post_id = resp_data.get("id") or resp_data.get("post_id")
                    success = response.get("status", False) and post_id is not None
                return ApprovalWorkflowService._facebook_result(success, post_id, response)

            # ── Facebook story ─────────────────────────────────────────────────
            elif post_type == "story":
                story_image_url = ApprovalWorkflowService._resolve_image_url(draft.get("image_url") or "")
                if not story_image_url:
                    return {"success": False, "error": "Facebook stories require an image. Re-generate with 'include_images: true'."}
                try:
                    return await ApprovalWorkflowService._publish_facebook_story(page_id, page_token, story_image_url)
                except Exception as story_err:
                    print(f"❌ FB story exception: {story_err}")
                    return {"success": False, "error": f"Facebook story publish failed. (Detail: {story_err})"}

            # ── Facebook feed (existing logic) ─────────────────────────────────
            else:
                post_data: Dict[str, Any] = {
                    "message": content,
                    "published": True,
                }

                image_url = draft.get("image_url")
                fb_video_url = draft.get("video_url") or ""
                if not image_url and fb_video_url:
                    return await ApprovalWorkflowService._publish_facebook_feed_video(
                        page_id, page_token, fb_video_url, content, scheduled_datetime,
                    )
                if image_url:
                    if image_url.startswith("data:"):
                        media_fbid = await ApprovalWorkflowService._upload_base64_image_to_facebook(
                            page_id=page_id,
                            page_token=page_token,
                            data_url=image_url,
                        )
                        if media_fbid:
                            post_data["attached_media"] = [{"media_fbid": media_fbid}]
                    else:
                        # Download the image, convert to JPEG, upload as binary multipart.
                        # URL-based upload fails silently for WebP and some CDN-gated URLs.
                        try:
                            import httpx as _httpx
                            from PIL import Image as _Image
                            import io as _io
                            async with _httpx.AsyncClient(timeout=30) as _fc:
                                _img_resp = await _fc.get(image_url)
                            _img = _Image.open(_io.BytesIO(_img_resp.content)).convert("RGB")
                            _buf = _io.BytesIO()
                            _img.save(_buf, format="JPEG", quality=92)
                            _jpeg_bytes = _buf.getvalue()
                            async with _httpx.AsyncClient(timeout=60) as _fc2:
                                _upload = await _fc2.post(
                                    f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/photos",
                                    data={"access_token": page_token, "published": "false"},
                                    files={"source": ("image.jpg", _jpeg_bytes, "image/jpeg")},
                                )
                            _fbid = _upload.json().get("id")
                            if _fbid:
                                post_data["attached_media"] = [{"media_fbid": _fbid}]
                                print(f"📸 FB image uploaded as JPEG binary: media_fbid={_fbid}")
                            else:
                                print(f"⚠️ FB image binary upload failed: {_upload.json()} — posting without image")
                        except Exception as _img_err:
                            print(f"⚠️ FB image upload error: {_img_err} — posting without image")

                if scheduled_datetime:
                    import calendar
                    post_data["published"] = False
                    post_data["scheduled_publish_time"] = int(
                        calendar.timegm(scheduled_datetime.utctimetuple())
                    )

                if post_data.get("attached_media"):
                    payload: Dict[str, Any] = {
                        "message": post_data["message"],
                        "published": post_data["published"],
                        "attached_media": post_data["attached_media"],
                    }
                    if post_data.get("scheduled_publish_time"):
                        payload["scheduled_publish_time"] = post_data["scheduled_publish_time"]
                    response = await FacebookService.publish_post(
                        page_id=page_id, payload=payload, access_token=page_token,
                    )
                else:
                    response = await FacebookService.post_on_facebook(
                        page_id=page_id, post_data=post_data, access_token=page_token,
                    )

                post_id = None
                success = False
                if isinstance(response, dict):
                    resp_data = response.get("responseData") or {}
                    post_id = resp_data.get("id") or resp_data.get("post_id")
                    success = response.get("status", False) and post_id is not None

                return ApprovalWorkflowService._facebook_result(success, post_id, response)

        return {"success": False, "error": f"Platform '{platform}' publishing not implemented yet"}

    @staticmethod
    async def _publish_facebook_feed_video(
        page_id: str,
        page_token: str,
        video_url: str,
        content: str,
        scheduled_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Post a user-uploaded video to the Page feed. Facebook fetches it from the
        public URL itself (file_url), so nothing is streamed through our server."""
        params: Dict[str, Any] = {
            "file_url": video_url,
            "description": content,
            "published": "true",
            "access_token": page_token,
        }
        if scheduled_datetime:
            import calendar
            params["published"] = "false"
            params["scheduled_publish_time"] = int(calendar.timegm(scheduled_datetime.utctimetuple()))
        import httpx
        print(f"🎬 FB video publish | page_id={page_id} video_url={video_url[:80]} scheduled={bool(scheduled_datetime)}")
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{page_id}/videos",
                    data=params,
                )
            data = resp.json() if resp.content else {}
        except Exception as e:
            print(f"❌ FB video publish exception: {e}")
            return {"success": False, "error": f"Facebook video publish failed. (Detail: {e})"}
        video_id = (data or {}).get("id")
        if video_id:
            print(f"✅ FB video publish: video_id={video_id}")
            return {"success": True, "post_id": video_id, "raw_response": data}
        err = (data or {}).get("error") or {}
        print(f"❌ FB video publish | status={resp.status_code} body={str(data)[:500]}")
        response = {
            "status": False,
            "responseCode": resp.status_code,
            "responseMessage": err.get("message") or "Facebook video publish failed",
            "meta_error": {
                "http_status": resp.status_code,
                "code": err.get("code"),
                "subcode": err.get("error_subcode"),
                "message": err.get("message"),
            },
        }
        result = ApprovalWorkflowService._facebook_result(False, None, response)
        result.setdefault("error", "Facebook video publish failed.")
        return result

# Usage Examples:
"""
# Approve and immediately publish
result = await ApprovalWorkflowService.approve_content(
    db=db,
    user_id="user_123",
    draft_ids=["draft_1", "draft_2"],
    schedule_option="immediate"
)

# Schedule for later
result = await ApprovalWorkflowService.approve_content(
    db=db,
    user_id="user_123", 
    draft_ids=["draft_3"],
    schedule_option="schedule",
    scheduled_datetime=datetime(2026, 3, 1, 10, 0, 0)
)

# Refine content
result = await ApprovalWorkflowService.refine_content(
    db=db,
    user_id="user_123",
    draft_id="draft_1",
    refinements={
        'content': 'Updated content with better call-to-action',
        'hashtags': ['UpdatedHashtag', 'BetterCTA'],
        'refinement_notes': 'Added stronger call-to-action'
    }
)
"""