"""
Proactively checking Outstand-managed social connections instead of waiting
for a publish attempt to fail.

Before this, a dead session was only ever caught reactively: when we tried
to publish and Outstand's error text matched a known "token invalidated"
signature. A business that simply hadn't tried to post since their session
died got no warning at all — confirmed live (October 2026): three Facebook
connections (ruumies.com, Chromite Agency Business Page, Jubilee Essentials)
sat connection_status="active" in our own database, untouched for months,
while Outstand's own dashboard had already been showing them as "needs
attention" the whole time. Two of the three had never been notified at all;
the third's one notification predated this specific disconnect by weeks.

Outstand's list_accounts response already carries the same signal their own
dashboard shows: an explicit isActive: false per account. social_account_
service.get_user_connections already reads this for the pull-based
Connected Accounts page — this module is the missing push side, asking on a
schedule instead of only when someone happens to look.
"""
from __future__ import annotations

from typing import Any, Dict, List

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.agents.social_media_manager.services.outstand_service import OutstandService


async def check_all_connections(db: AsyncIOMotorDatabase, limit: int = 1000) -> Dict[str, int]:
    """Groups active Outstand-managed connections by tenant — one
    list_accounts call per tenant, not per connection, since Outstand rate-
    limits/bills per call — and marks+notifies any whose session Outstand
    now reports as no longer active."""
    from app.agents.social_media_manager.services.approval_workflow_service import (
        _mark_connection_disconnected_and_notify,
    )

    connections = await db["social_connections"].find({
        "connected_via": "outstand",
        "connection_status": "active",
        "outstand_account_id": {"$exists": True, "$ne": None},
    }).to_list(length=limit)

    by_tenant: Dict[str, List[Dict[str, Any]]] = {}
    for c in connections:
        # "user_id" holds whichever id was actually used as the Outstand
        # tenant_id at connect time (the real user_id for a personal brand,
        # the brand_id for an agency brand — see social_account_service.
        # initiate_connection_flow) — the same overloaded convention every
        # other list_accounts call site in this codebase already relies on.
        tenant = c.get("user_id")
        if tenant:
            by_tenant.setdefault(tenant, []).append(c)

    outstand = OutstandService()
    checked = flagged = errors = 0
    for tenant, conns in by_tenant.items():
        try:
            result = await outstand.list_accounts(tenant_id=tenant)
        except Exception as e:
            print(f"⚠️ connection_health_check: list_accounts failed for tenant={tenant}: {e}")
            errors += 1
            continue
        live_by_id = {acc.get("id"): acc for acc in result.get("data", [])}
        for c in conns:
            checked += 1
            acc = live_by_id.get(c.get("outstand_account_id"))
            # Only an explicit isActive: false is a real signal worth acting
            # on — an account missing from the response, or the field simply
            # absent, is not treated as dead (same convention already
            # established in get_user_connections, to avoid flagging a
            # connection as broken just because Outstand's response happened
            # not to include it this time).
            if acc is not None and acc.get("isActive") is False:
                await _mark_connection_disconnected_and_notify(
                    db=db, user_id=c.get("user_id"), platform=c.get("platform", "unknown"),
                    connection=c, reason="token invalidated (detected via proactive connection health check)",
                )
                flagged += 1
    return {"checked": checked, "flagged": flagged, "errors": errors}
