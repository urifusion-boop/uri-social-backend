# app/agents/social_media_manager/services/approval_workflow_service.py

from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
import asyncio
import base64
import re
import requests

from app.domain.responses.uri_response import UriResponse
from app.services.FacebookService import FacebookService
from app.core.config import settings
from app.agents.social_media_manager.services.outstand_service import OutstandService


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
        approval_notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Approve content drafts and optionally schedule them
        
        Args:
            draft_ids: List of draft IDs to approve
            schedule_option: 'immediate', 'schedule', or 'save_draft'
            scheduled_datetime: When to publish (if schedule_option is 'schedule')
            approval_notes: Optional notes from approver
        """
        try:
            print(f"✅ approve_content | user_id={user_id} draft_ids={draft_ids} schedule_option={schedule_option}")
            approved_drafts = []
            errors = []

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
                        update_data["scheduled_date"] = scheduled_datetime or (datetime.utcnow() + timedelta(hours=1))
                        update_data["status"] = "scheduled"
                        update_data["user_id"] = user_id  # Ensure user_id is on the draft for the scheduler
                    elif schedule_option == "immediate":
                        # Mark for immediate publishing
                        update_data["status"] = "ready_to_publish"
                    
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": update_data}
                    )
                    
                    approved_drafts.append({
                        "draft_id": draft_id,
                        "platform": draft["platform"],
                        "status": update_data["status"],
                        "scheduled_date": scheduled_datetime.isoformat() if scheduled_datetime else None
                    })
                    
                except Exception as e:
                    errors.append({"draft_id": draft_id, "error": str(e)})
            
            # Publish immediately or send to Outstand with scheduledAt
            if schedule_option in ("immediate", "schedule") and approved_drafts:
                publish_results = await ApprovalWorkflowService._trigger_immediate_publishing(
                    db, user_id, [d["draft_id"] for d in approved_drafts],
                    scheduled_datetime=scheduled_datetime if schedule_option == "schedule" else None,
                )

                # Update drafts with publishing results
                for draft in approved_drafts:
                    draft_result = publish_results.get(draft["draft_id"])
                    if draft_result:
                        draft["publish_result"] = draft_result
            
            return UriResponse.get_single_data_response("content_approval", {
                "approved_drafts": approved_drafts,
                "errors": errors,
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
            
            return UriResponse.get_single_data_response("content_refinement", {
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
        """
        try:
            from .content_generation_service import ContentGenerationService
            
            # Get original draft details
            draft = await db["content_drafts"].find_one({"id": draft_id})
            if not draft:
                return UriResponse.error_response("Draft not found")
            
            # Get original request
            request = await db["content_requests"].find_one({"id": draft["request_id"]})
            if not request:
                return UriResponse.error_response("Original request not found")
            
            # Generate new content with feedback
            enhanced_seed = request["seed_content"]
            if regeneration_feedback:
                enhanced_seed += f"\n\nUser feedback: {regeneration_feedback}"
            
            result = await ContentGenerationService._generate_platform_content(
                platform=draft["platform"],
                seed_content=enhanced_seed,
                request_id=draft["request_id"],
                user_id=user_id
            )
            
            if result.get('status'):
                new_draft_data = result['responseData']
                
                # Create new draft with regenerated content
                new_draft_id = str(ObjectId())
                new_draft = {
                    "id": new_draft_id,
                    "request_id": draft["request_id"],
                    "platform": draft["platform"],
                    "content": new_draft_data["content"],
                    "original_content": new_draft_data["content"],
                    "hashtags": new_draft_data.get("hashtags", []),
                    "status": "regenerated",
                    "regenerated_from": draft_id,
                    "regeneration_feedback": regeneration_feedback,
                    "ai_metadata": new_draft_data.get("ai_metadata"),
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
                
                await db["content_drafts"].insert_one(new_draft)
                
                # Mark original as replaced
                await db["content_drafts"].update_one(
                    {"id": draft_id},
                    {"$set": {
                        "status": "replaced",
                        "replaced_by": new_draft_id,
                        "updated_at": datetime.utcnow()
                    }}
                )
                
                return UriResponse.get_single_data_response("content_regeneration", {
                    "original_draft_id": draft_id,
                    "new_draft_id": new_draft_id,
                    "new_content": new_draft_data["content"],
                    "hashtags": new_draft_data.get("hashtags", []),
                    "regeneration_feedback": regeneration_feedback,
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
            # Find content scheduled for now or past
            current_time = datetime.utcnow()
            
            scheduled_content = await db["content_drafts"].find({
                "status": "scheduled",
                "scheduled_date": {"$lte": current_time}
            }).to_list(length=100)
            
            if not scheduled_content:
                return {"message": "No scheduled content to publish", "published": 0}
            
            published_count = 0
            errors = []
            
            for draft in scheduled_content:
                try:
                    # Use user_id stored directly on the draft
                    draft_user_id = draft.get("user_id")
                    if not draft_user_id:
                        # Fallback: derive from linked content request
                        request_doc = await db["content_requests"].find_one({"id": draft.get("request_id")})
                        draft_user_id = request_doc["user_id"] if request_doc else None

                    if not draft_user_id:
                        errors.append({"draft_id": draft["id"], "error": "Cannot determine user_id for draft"})
                        continue

                    connections_cursor = db["social_connections"].find({
                        "user_id": draft_user_id,
                        "platform": draft["platform"],
                        "connection_status": "active",
                    }).sort("created_at", -1).limit(1)
                    conn_list = await connections_cursor.to_list(length=1)
                    connection = conn_list[0] if conn_list else None

                    # Outstand live-lookup fallback (same as _trigger_immediate_publishing)
                    if not connection:
                        try:
                            from app.agents.social_media_manager.services.outstand_service import OutstandService, PLATFORM_TO_NETWORK
                            outstand = OutstandService()
                            network = PLATFORM_TO_NETWORK.get(draft["platform"], draft["platform"])
                            live_result = await outstand.list_accounts(tenant_id=draft_user_id, network=network)
                            live_accounts = live_result.get("data", [])
                            if live_accounts:
                                acc = live_accounts[0]
                                connection = {
                                    "user_id": draft_user_id,
                                    "platform": draft["platform"],
                                    "outstand_account_id": acc.get("id"),
                                    "connected_via": "outstand",
                                    "connection_status": "active",
                                }
                                doc = {
                                    "user_id": draft_user_id,
                                    "platform": network,
                                    "outstand_account_id": acc.get("id"),
                                    "username": acc.get("username"),
                                    "account_name": acc.get("nickname") or acc.get("username"),
                                    "connection_status": "active",
                                    "connected_via": "outstand",
                                    "connected_at": datetime.utcnow(),
                                    "updated_at": datetime.utcnow(),
                                }
                                await db["social_connections"].replace_one(
                                    {"user_id": draft_user_id, "platform": network, "outstand_account_id": acc.get("id")},
                                    doc, upsert=True,
                                )
                        except Exception as e:
                            print(f"❌ Outstand fallback lookup failed for scheduled post: {e}")

                    if not connection:
                        errors.append({
                            "draft_id": draft["id"],
                            "error": f"No active connection for {draft['platform']}",
                        })
                        continue

                    print(f"🕐 Publishing scheduled post | draft_id={draft['id']} platform={draft['platform']} user_id={draft_user_id}")
                    publish_result = await ApprovalWorkflowService._publish_to_platform(
                        platform=draft["platform"],
                        draft=draft,
                        connection=connection,
                        scheduled_datetime=None,  # We publish immediately when the time arrives
                        db=db,
                    )

                    conn_filter = (
                        {"id": connection["id"]} if connection.get("id")
                        else {"user_id": draft_user_id, "outstand_account_id": connection.get("outstand_account_id")}
                    )
                    if publish_result.get("success"):
                        await db["content_drafts"].update_one(
                            {"id": draft["id"]},
                            {"$set": {
                                "status": "published",
                                "published_date": datetime.utcnow(),
                                "platform_post_id": publish_result.get("post_id"),
                                "publish_response": publish_result.get("raw_response"),
                                "updated_at": datetime.utcnow(),
                            }},
                        )
                        await db["social_connections"].update_one(conn_filter, {"$inc": {"total_posts_published": 1}})
                        published_count += 1
                    else:
                        await db["content_drafts"].update_one(
                            {"id": draft["id"]},
                            {"$set": {
                                "status": "publish_failed",
                                "error_message": publish_result.get("error"),
                                "updated_at": datetime.utcnow(),
                            }},
                        )
                        await db["social_connections"].update_one(conn_filter, {"$inc": {"total_publish_errors": 1}})
                        errors.append({"draft_id": draft["id"], "error": publish_result.get("error")})

                except Exception as e:
                    errors.append({"draft_id": draft.get("id", "unknown"), "error": str(e)})
            
            return {
                "published_count": published_count,
                "errors": errors,
                "processed_at": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            return {"error": f"Scheduled publishing failed: {str(e)}"}
    
    @staticmethod
    async def _trigger_immediate_publishing(
        db: AsyncIOMotorDatabase,
        user_id: str,
        draft_ids: List[str],
        scheduled_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Publish approved drafts via Outstand. Pass scheduled_datetime to schedule
        via Outstand's native scheduledAt support instead of publishing immediately.
        """
        results = {}

        for draft_id in draft_ids:
            try:
                draft = await db["content_drafts"].find_one({"id": draft_id})
                if not draft:
                    results[draft_id] = {"success": False, "error": "Draft not found"}
                    continue

                platform = draft["platform"]
                print(f"🚀 Immediate publish | draft_id={draft_id} platform={platform} user_id={user_id}")

                connections_cursor = db["social_connections"].find({
                    "user_id": user_id,
                    "platform": platform,
                    "connection_status": "active",
                }).sort("created_at", -1).limit(1)
                connection = await connections_cursor.to_list(length=1)
                connection = connection[0] if connection else None

                # Fall back to Outstand live lookup if local mirror is empty
                if not connection:
                    print(f"⚠️ No local {platform} connection for user_id={user_id}, querying Outstand directly...")
                    try:
                        from app.agents.social_media_manager.services.outstand_service import OutstandService, PLATFORM_TO_NETWORK
                        outstand = OutstandService()
                        network = PLATFORM_TO_NETWORK.get(platform, platform)
                        live_result = await outstand.list_accounts(tenant_id=user_id, network=network)
                        live_accounts = live_result.get("data", [])
                        if live_accounts:
                            acc = live_accounts[0]
                            connection = {
                                "user_id": user_id,
                                "platform": platform,
                                "outstand_account_id": acc.get("id"),
                                "connected_via": "outstand",
                                "connection_status": "active",
                            }
                            print(f"✅ Found Outstand account: {acc.get('id')} ({acc.get('username')})")
                            # Sync to local DB so future lookups work
                            from datetime import datetime as _dt
                            doc = {
                                "user_id": user_id,
                                "platform": network,
                                "outstand_account_id": acc.get("id"),
                                "username": acc.get("username"),
                                "account_name": acc.get("nickname") or acc.get("username"),
                                "profile_picture_url": acc.get("profile_picture_url"),
                                "account_type": acc.get("accountType"),
                                "network_unique_id": acc.get("network_unique_id") or acc.get("networkUniqueId"),
                                "connection_status": "active",
                                "connected_via": "outstand",
                                "connected_at": _dt.utcnow(),
                                "updated_at": _dt.utcnow(),
                            }
                            await db["social_connections"].replace_one(
                                {"user_id": user_id, "platform": network, "outstand_account_id": acc.get("id")},
                                doc,
                                upsert=True,
                            )
                            print(f"💾 Synced missing connection to local DB")
                    except Exception as e:
                        print(f"❌ Outstand fallback lookup failed: {e}")

                if not connection:
                    print(f"❌ No active {platform} connection found for user_id={user_id}")
                    results[draft_id] = {
                        "success": False,
                        "error": f"No active {platform} connection. Connect your account first.",
                    }
                    continue

                print(f"🔗 Using connection: connected_via={connection.get('connected_via')} outstand_account_id={connection.get('outstand_account_id')}")
                publish_result = await ApprovalWorkflowService._publish_to_platform(
                    platform=platform,
                    draft=draft,
                    connection=connection,
                    scheduled_datetime=scheduled_datetime,
                    db=db,
                )
                print(f"📊 Publish result for draft_id={draft_id}: {publish_result}")

                conn_filter = (
                    {"id": connection["id"]} if connection.get("id")
                    else {"user_id": user_id, "outstand_account_id": connection.get("outstand_account_id")}
                )
                if publish_result.get("success"):
                    is_scheduled = scheduled_datetime is not None
                    await db["content_drafts"].update_one(
                        {"id": draft_id},
                        {"$set": {
                            "status": "scheduled" if is_scheduled else "published",
                            "published_date": None if is_scheduled else datetime.utcnow(),
                            "scheduled_date": scheduled_datetime if is_scheduled else None,
                            "platform_post_id": publish_result.get("post_id"),
                            "publish_response": publish_result.get("raw_response"),
                            "updated_at": datetime.utcnow(),
                        }},
                    )
                    await db["social_connections"].update_one(conn_filter, {"$inc": {"total_posts_published": 1}})
                else:
                    await db["social_connections"].update_one(conn_filter, {"$inc": {"total_publish_errors": 1}})

                results[draft_id] = publish_result

            except Exception as e:
                print(f"❌ Publish exception for draft_id={draft_id}: {e}")
                results[draft_id] = {"success": False, "error": str(e)}

        return results

    @staticmethod
    async def _upload_base64_to_imgbb(data_url: str) -> Optional[str]:
        """
        Upload a base64 data URL image to imgBB and return a permanent public URL.
        This is needed because Outstand must fetch the image from a public URL,
        and our ngrok/local URLs are not reliably accessible to Outstand's servers.
        Returns the public URL string or None on failure.
        """
        import httpx as _httpx
        api_key = settings.IMGBB_API_KEY
        if not api_key:
            print("⚠️ IMGBB_API_KEY not set — cannot upload image for Outstand")
            return None

        try:
            match = re.match(r'data:[^;]+;base64,(.+)', data_url, re.DOTALL)
            if not match:
                print("⚠️ imgBB upload: invalid data URL format")
                return None
            b64_data = match.group(1)

            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.imgbb.com/1/upload",
                    data={"key": api_key, "image": b64_data},
                )
                resp_json = resp.json()

            if resp_json.get("success"):
                url = resp_json["data"]["url"]
                print(f"📸 Image uploaded to imgBB: {url}")
                return url
            else:
                print(f"⚠️ imgBB upload failed: {resp_json}")
                return None
        except Exception as e:
            print(f"⚠️ imgBB upload exception: {e}")
            return None

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
        content = draft["content"]
        if draft.get("hashtags"):
            tags = " ".join(f"#{t.strip('#')}" for t in draft["hashtags"])
            content = f"{content} {tags}"

        # ── Outstand-connected accounts ───────────────────────────────────────
        if connection.get("connected_via") == "outstand":
            try:
                outstand = OutstandService()
                scheduled_at = None
                if scheduled_datetime:
                    scheduled_at = scheduled_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")

                # Build a public image URL for Outstand.
                # DALL-E images are stored as base64 data URLs; upload to imgBB so Outstand
                # can always fetch them (ngrok/localhost URLs are not reliably accessible to Outstand).
                image_url = draft.get("image_url") or ""
                media_urls = None
                if image_url:
                    if image_url.startswith("data:"):
                        public_image_url = await ApprovalWorkflowService._upload_base64_to_imgbb(image_url)
                        if public_image_url:
                            media_urls = [public_image_url]
                            # Cache the public URL back to the draft so we don't re-upload on retry
                            await db["content_drafts"].update_one(
                                {"id": draft["id"]},
                                {"$set": {"image_url": public_image_url, "updated_at": datetime.utcnow()}},
                            )
                    else:
                        media_urls = [image_url]

                # Instagram requires at least one image — warn and skip if no media
                if platform == "instagram" and not media_urls:
                    print(f"⚠️ Instagram post skipped — Instagram API requires an image. Generate content with 'include_images: true' to post on Instagram.")
                    return {"success": False, "error": "Instagram requires an image. Re-generate this post with 'include_images: true' enabled."}

                # For X/Twitter threads, pass the individual tweets so each becomes
                # its own Outstand container (native thread support).
                tweets = None
                if platform in ("twitter", "x") and draft.get("is_twitter_thread") and draft.get("tweets"):
                    tweets = draft["tweets"]

                print(f"📤 Publishing via Outstand | account_id={connection.get('outstand_account_id')} platform={platform} has_image={bool(media_urls)} thread={bool(tweets and len(tweets) > 1)}")
                result = await outstand.publish_post(
                    outstand_account_ids=[connection["outstand_account_id"]],
                    content=content,
                    scheduled_at=scheduled_at,
                    media_urls=media_urls,
                    tweets=tweets,
                )
                print(f"📬 Outstand publish response: {result}")

                # Outstand returns {"data": {"id": "..."}} or {"success": true, "post": {"id": "..."}}
                post_obj = result.get("post") or result.get("data") or {}
                if isinstance(post_obj, list):
                    post_obj = post_obj[0] if post_obj else {}
                post_id = post_obj.get("id")
                # Treat a returned post ID as success (Outstand may omit "success" key)
                success = post_id is not None
                if not success:
                    print(f"⚠️ Outstand returned no post ID — possible publish failure: {result}")
                return {"success": success, "post_id": post_id, "raw_response": result}
            except Exception as e:
                print(f"❌ Outstand publish exception: {e}")
                return {"success": False, "error": f"Outstand publish failed: {str(e)}"}

        # ── Legacy direct Facebook connection ─────────────────────────────────
        if platform == "facebook":
            page_id = connection.get("page_id")
            page_token = connection.get("page_access_token")

            if not page_id or not page_token:
                return {"success": False, "error": "No valid Facebook credentials on this connection. Reconnect your account."}

            post_data: Dict[str, Any] = {
                "message": content,
                "published": True,
            }

            image_url = draft.get("image_url")
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
                    post_data["media"] = [{"url": image_url, "media_type": "IMAGE"}]

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

            return {"success": success, "post_id": post_id, "raw_response": response}

        return {"success": False, "error": f"Platform '{platform}' publishing not implemented yet"}


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