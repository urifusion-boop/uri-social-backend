"""
Unified Inbox — which accounts this workspace actually owns.

Two jobs, and they use DIFFERENT ids, which is the whole reason this file exists:

· **Routing an incoming webhook.** Meta keys `entry.id` on the Instagram professional
  account for Instagram, and on the Page for Messenger and Page comments. Look up the
  wrong one and a real customer message is dropped as "unconnected account".

· **Sending a reply.** Both platforms send through the PAGE with the page token. An
  Instagram reply posted to the Instagram user id fails.

So a channel account records both, and never conflates them. Tokens are reused from
the connection the user already made for publishing — a second OAuth prompt for the
same Page would be asking the user to grant what they have already granted.
"""
from __future__ import annotations

from typing import Any, Optional

from app.models.brand_account import BrandAccount

from .entities import CHANNEL_ACCOUNTS, Platform, now

SOCIAL_CONNECTIONS = "social_connections"


def _workspace_of(conn: dict) -> str:
    """The workspace a connection belongs to.

    A personal connection carries no brand_id, and the workspace is NOT the bare
    user id — it is the deterministic personal brand id the rest of the app uses.
    Comparing against the raw user id silently matches nothing.
    """
    brand_id = conn.get("brand_id")
    if brand_id:
        return brand_id
    return BrandAccount.personal_brand_id(conn.get("user_id") or "")


def channel_rows(conn: dict, workspace_id: str = "") -> list[dict]:
    """Turn one social connection into the channel accounts it provides.

    An Instagram connection provides TWO: the Instagram account (DMs and comments
    arrive under its id) and the Page behind it (Messenger and Page comments arrive
    under the Page's). Registering only one silently loses half the inbox.
    """
    token = conn.get("page_access_token") or ""
    page_id = str(conn.get("page_id") or "")
    ig_user_id = str(conn.get("ig_user_id") or "")
    workspace_id = workspace_id or _workspace_of(conn)
    if not token:
        return []

    rows: list[dict] = []
    if ig_user_id:
        rows.append({
            "workspace_id": workspace_id,
            "platform": Platform.INSTAGRAM.value,
            "external_account_id": ig_user_id,   # what webhooks key on
            "page_id": page_id,                  # what replies are sent through
            "access_token": token,
            "name": conn.get("username") or conn.get("account_name") or "",
        })
    if page_id:
        rows.append({
            "workspace_id": workspace_id,
            "platform": Platform.FACEBOOK.value,
            "external_account_id": page_id,
            "page_id": page_id,
            "access_token": token,
            "name": conn.get("page_name") or conn.get("account_name") or "",
        })
    return rows


async def link_workspace_channels(db, user_id: str, workspace_id: str) -> dict:
    """Register this workspace's connected accounts with the inbox. Safe to re-run.

    Re-running is the normal case, not an edge one: tokens are refreshed and Pages are
    reconnected, and the inbox must pick up the new token without creating a second
    row that webhooks might match instead.
    """
    q: dict[str, Any] = {"connection_status": "active",
                         "platform": {"$in": ["instagram", "facebook"]}}
    conns = await db[SOCIAL_CONNECTIONS].find(q).to_list(200)

    linked: list[dict] = []
    # Why a link found nothing matters: no connections at all is a different
    # problem from connections that belong to another workspace, and without
    # this the caller cannot tell them apart.
    skipped: list[str] = []
    for conn in conns:
        if _workspace_of(conn) != workspace_id:
            skipped.append(_workspace_of(conn))
            continue
        for row in channel_rows(conn, workspace_id):
            key = {"workspace_id": row["workspace_id"],
                   "platform": row["platform"],
                   "external_account_id": row["external_account_id"]}
            await db[CHANNEL_ACCOUNTS].update_one(
                key,
                {"$set": {**row, "updated_at": now()},
                 "$setOnInsert": {"connected_at": now()}},
                upsert=True,
            )
            linked.append(key)

    # Subscribe each distinct Page once, after the rows are stored.
    subscriptions: list[dict] = []
    seen_pages: set[str] = set()
    for conn in conns:
        if _workspace_of(conn) != workspace_id:
            continue
        page_id = str(conn.get("page_id") or "")
        token = conn.get("page_access_token") or ""
        if not page_id or not token or page_id in seen_pages:
            continue
        seen_pages.add(page_id)

        page_token = await resolve_page_token(page_id, token)
        if page_token != token:
            # Store the Page's own token: replies are Page-scoped too, so a user
            # token here fails every send with the same (#210).
            await db[CHANNEL_ACCOUNTS].update_many(
                {"workspace_id": workspace_id, "page_id": page_id},
                {"$set": {"access_token": page_token, "updated_at": now()}},
            )

        ok, err = await subscribe_page_to_app(page_id, page_token)
        subscriptions.append({"page_id": page_id, "subscribed": ok, "error": err})
        if not ok:
            print(f"[Inbox] could not subscribe page {page_id}: {err}", flush=True)

    if not linked:
        print(f"[Inbox] linked nothing for {workspace_id!r}: "
              f"{len(conns)} connection(s) considered, "
              f"{len(skipped)} in other workspaces {sorted(set(skipped))[:5]}", flush=True)
    # Returned, not stashed on the module: a global would be shared across
    # requests and hand one workspace's page ids to the next caller.
    return {"linked": linked, "subscriptions": subscriptions}


async def resolve_page_token(page_id: str, token: str) -> str:
    """The Page's OWN access token, given whatever token we have stored.

    Connections do not all store the same thing under page_access_token: some
    paths save a USER token, and Meta answers "(#210) A page access token is
    required" to anything Page-scoped. Asking the Page for its own token works
    whichever kind we started with, and the answer is what both subscribing and
    replying need.
    """
    import httpx

    from app.core.config import settings

    if not (page_id and token):
        return token

    version = getattr(settings, "FACEBOOK_API_VERSION", "") or "v21.0"
    url = f"https://graph.facebook.com/{version}/{page_id}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, params={"fields": "access_token",
                                              "access_token": token})
        if r.status_code < 400:
            return r.json().get("access_token") or token
    except Exception as e:
        print(f"[Inbox] could not resolve a page token for {page_id}: {e}", flush=True)
    return token


async def subscribe_page_to_app(page_id: str, access_token: str) -> tuple[bool, str]:
    """Subscribe the Page to this app so Meta actually delivers its events.

    Ticking fields in the App Dashboard is NOT enough: that configures which
    fields the app may receive, while this says the Page consents to sending
    them. Miss it and everything looks correctly configured and nothing arrives.

    Meta rejects the WHOLE call if any one field needs a permission the token
    lacks, so a missing pages_messaging would also cost us comments. Falling back
    to the fields that do work means half a working inbox instead of none, and
    the returned message still names what was dropped.
    """
    import httpx

    from app.core.config import settings

    version = getattr(settings, "FACEBOOK_API_VERSION", "") or "v21.0"
    url = f"https://graph.facebook.com/{version}/{page_id}/subscribed_apps"

    async def attempt(fields: str) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    url, data={"subscribed_fields": fields, "access_token": access_token})
        except Exception as e:
            return False, str(e)[:200]
        if r.status_code >= 400:
            try:
                return False, (r.json().get("error") or {}).get("message", "")[:300]
            except ValueError:
                return False, r.text[:300]
        return True, ""

    full = "messages,messaging_postbacks,message_reactions,feed"
    ok, err = await attempt(full)
    if ok:
        return True, ""

    # Only the messaging fields need pages_messaging; comments ride on feed.
    if "pages_messaging" in err or "(#200)" in err:
        ok_partial, err_partial = await attempt("feed")
        if ok_partial:
            return True, ("comments only — the token lacks pages_messaging, so DMs "
                          "will not be delivered until that scope is granted")
        return False, f"{err} | feed-only also failed: {err_partial}"

    return False, err
async def fetch_contact_name(external_user_id: str, access_token: str) -> str:
    """The sender's name, which the DM webhook does not include.

    Instagram sends only a scoped user id, so an inbox built purely from webhook
    payloads shows every customer as "Unknown contact". Best effort: a failure
    here leaves the name blank rather than holding up the message.
    """
    import httpx

    from app.core.config import settings

    if not (external_user_id and access_token):
        return ""

    version = getattr(settings, "FACEBOOK_API_VERSION", "") or "v21.0"
    url = f"https://graph.facebook.com/{version}/{external_user_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params={"fields": "name,username",
                                              "access_token": access_token})
        if r.status_code < 400:
            body = r.json()
            return body.get("name") or body.get("username") or ""
        print(f"[Inbox] could not fetch a name for {external_user_id}: "
              f"{r.status_code}", flush=True)
    except Exception as e:
        print(f"[Inbox] could not fetch a name for {external_user_id}: {e}", flush=True)
    return ""


async def account_for_event(db, external_account_id: str) -> Optional[dict]:
    """The channel account a webhook belongs to, or None if nobody connected it."""
    if not external_account_id:
        return None
    return await db[CHANNEL_ACCOUNTS].find_one(
        {"external_account_id": str(external_account_id)})
