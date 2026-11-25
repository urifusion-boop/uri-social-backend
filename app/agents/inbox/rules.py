"""
Automation rules — what URI says, where, and when, as the owner wrote it.

A rule is a trigger (words, example questions, or any message), an optional ad it is
limited to, and one or two fixed responses: a public comment reply, a private DM, or
both (comment-to-DM). Rules run BEFORE the AI agent: a rule that matches answers, and
the agent stays out of it. No rule matches, the agent carries on as before.

Rule replies are the owner's own words — no model writes them, nothing is billed.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from bson import ObjectId

from .entities import (
    CONVERSATIONS, IDENTITIES, MESSAGES, OUTBOX,
    Delivery, Direction, Kind, now,
)
from .send import ProviderTimeout, SendRefused

RULES = "inbox_rules"
RULE_FIRES = "inbox_rule_fires"

KINDS = ("comment", "dm", "comment_to_dm")
MAX_RULES = 100
MAX_TEXT = 1000
MAX_LIST = 30
COMMENT_COOLDOWN = timedelta(hours=24)
DM_RULE_REPLIES_PER_HOUR = 5

Llm = Callable[[str, str], Awaitable[dict]]


class RuleError(ValueError):
    pass


def _strings(value: Any, limit: int = MAX_LIST, size: int = 200) -> list[str]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = str(item or "").strip()[:size]
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
    return out[:limit]


def normalize_rule(raw: dict) -> dict:
    """The rule as it is stored, or RuleError saying what is missing."""
    raw = raw or {}
    kind = raw.get("kind")
    if kind not in KINDS:
        raise RuleError("Pick what this rule responds to.")
    match_raw = raw.get("match") or {}
    match = {"keywords": _strings(match_raw.get("keywords")),
             "examples": _strings(match_raw.get("examples"), size=300),
             "any": bool(match_raw.get("any"))}
    ad_ids = _strings(raw.get("ad_ids"), size=64)
    if not (match["keywords"] or match["examples"] or (match["any"] and ad_ids)):
        raise RuleError("Add at least one word or example question for URI to look out for.")
    public_reply = str(raw.get("public_reply") or "").strip()[:MAX_TEXT]
    dm = str(raw.get("dm") or "").strip()[:MAX_TEXT]
    if kind == "comment" and not public_reply:
        raise RuleError("Write the public reply.")
    if kind in ("dm", "comment_to_dm") and not dm:
        raise RuleError("Write the private message.")
    platforms = [p for p in _strings(raw.get("platforms"), size=20) if p in ("instagram", "facebook")]
    name = str(raw.get("name") or "").strip()[:80] or (match["keywords"] or match["examples"] or ["Any message"])[0]
    return {"kind": kind, "name": name, "enabled": bool(raw.get("enabled", True)), "match": match,
            "public_reply": public_reply if kind != "dm" else "",
            "dm": dm if kind != "comment" else "",
            "ad_ids": ad_ids, "platforms": platforms}


def public_rule(doc: dict) -> dict:
    return {"id": doc["rule_id"], **{k: doc.get(k) for k in (
        "kind", "name", "enabled", "match", "public_reply", "dm", "ad_ids", "platforms")},
        "fired_count": doc.get("fired_count", 0),
        "last_fired_at": doc["last_fired_at"].isoformat() if doc.get("last_fired_at") else None}


async def list_rules(db, workspace_id: str, only_enabled: bool = False) -> list[dict]:
    query: dict[str, Any] = {"workspace_id": workspace_id}
    if only_enabled:
        query["enabled"] = True
    return await db[RULES].find(query).sort("created_at", 1).to_list(MAX_RULES)


async def create_rule(db, workspace_id: str, raw: dict, actor_id: str) -> dict:
    clean = normalize_rule(raw)
    if await db[RULES].count_documents({"workspace_id": workspace_id}) >= MAX_RULES:
        raise RuleError(f"You can have up to {MAX_RULES} rules.")
    doc = {**clean, "rule_id": uuid.uuid4().hex, "workspace_id": workspace_id,
           "fired_count": 0, "created_by": actor_id, "created_at": now(), "updated_at": now()}
    await db[RULES].insert_one(dict(doc))
    return public_rule(doc)


async def update_rule(db, workspace_id: str, rule_id: str, raw: dict) -> Optional[dict]:
    existing = await db[RULES].find_one({"workspace_id": workspace_id, "rule_id": rule_id})
    if not existing:
        return None
    merged = {**public_rule(existing), **raw}
    clean = normalize_rule(merged)
    await db[RULES].update_one({"_id": existing["_id"]}, {"$set": {**clean, "updated_at": now()}})
    return public_rule({**existing, **clean})


async def delete_rule(db, workspace_id: str, rule_id: str) -> bool:
    res = await db[RULES].delete_one({"workspace_id": workspace_id, "rule_id": rule_id})
    return res.deleted_count > 0


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9₦]+", " ", folded.lower())).strip() + " "


def keyword_match(rule: dict, text: str) -> bool:
    haystack = _norm(text)
    return any(_norm(k) in haystack for k in rule["match"]["keywords"] if _norm(k).strip())


def applies_to(rule: dict, conv: dict) -> bool:
    """The rule's channel and ad limits, before any wording is looked at."""
    if rule["platforms"] and conv.get("platform") not in rule["platforms"]:
        return False
    if rule["ad_ids"] and conv.get("source_ad_id") not in rule["ad_ids"]:
        return False
    return True


async def ai_match(text: str, candidates: list[dict], llm: Llm) -> Optional[dict]:
    """The one rule whose example questions this message is a variation of, or None."""
    listing = [{"id": r["rule_id"], "examples": r["match"]["examples"]} for r in candidates]
    system = ("You match a customer's message to the rule whose example questions it is a "
              "variation of (same intent, different wording, any language incl. Nigerian "
              "Pidgin). Reply JSON {\"id\": \"<rule id>\"} or {\"id\": null} when none clearly fit. "
              "Never match on a loosely related topic.")
    try:
        answer = await llm(system, f"Message: {text}\nRules: {listing}")
    except Exception as e:
        print(f"[Rules] AI match failed: {type(e).__name__}", flush=True)
        return None
    chosen = (answer or {}).get("id")
    return next((r for r in candidates if r["rule_id"] == chosen), None)


async def find_rule(rules: list[dict], conv: dict, text: str, llm: Llm) -> Optional[dict]:
    usable = [r for r in rules if applies_to(r, conv)]
    for r in usable:
        if keyword_match(r, text) or (r["match"]["any"] and r["ad_ids"]):
            return r
    with_examples = [r for r in usable if r["match"]["examples"]]
    return await ai_match(text, with_examples, llm) if with_examples else None


def _fill(text: str, first_name: str) -> str:
    return text.replace("{first_name}", first_name or "there")


async def _cooling_down(db, workspace_id: str, rule: dict, conv: dict, contact: str) -> bool:
    since = now() - (COMMENT_COOLDOWN if conv.get("kind") == Kind.COMMENT.value else timedelta(hours=1))
    query = {"workspace_id": workspace_id, "at": {"$gte": since}}
    if conv.get("kind") == Kind.COMMENT.value:
        return bool(await db[RULE_FIRES].find_one(
            {**query, "rule_id": rule["rule_id"], "contact": contact, "thread": conv.get("external_thread_id")}))
    return await db[RULE_FIRES].count_documents(
        {**query, "conversation_id": str(conv["_id"])}) >= DM_RULE_REPLIES_PER_HOUR


async def _private_reply(db, workspace_id: str, rule: dict, conv: dict, account: dict, comment: dict,
                         contact: dict, text: str, private_transport) -> bool:
    """The DM half of comment-to-DM, once per comment, filed in the commenter's DM thread."""
    key = f"rule:{rule['rule_id']}:dm:{comment['provider_message_id']}"
    if await db[OUTBOX].find_one({"workspace_id": workspace_id, "idempotency_key": key}):
        return False
    outbox = await db[OUTBOX].insert_one({
        "workspace_id": workspace_id, "conversation_id": str(conv["_id"]), "idempotency_key": key,
        "actor_id": "uri-agent", "text": text, "delivery": Delivery.PENDING.value, "created_at": now()})
    try:
        sent = await private_transport(account, comment["provider_message_id"], text)
    except ProviderTimeout as e:
        await db[OUTBOX].update_one({"_id": outbox.inserted_id}, {"$set": {
            "delivery": Delivery.UNKNOWN.value, "failure_reason": str(e) or "provider timeout"}})
        return False
    except Exception as e:
        await db[OUTBOX].update_one({"_id": outbox.inserted_id}, {"$set": {
            "delivery": Delivery.FAILED.value, "failure_reason": str(e)[:500]}})
        return False
    await db[OUTBOX].update_one({"_id": outbox.inserted_id}, {"$set": {
        "delivery": Delivery.ACCEPTED.value, "provider_message_id": sent.get("message_id", "")}})
    await _file_private_reply(db, workspace_id, conv, account, contact, sent, text)
    return True


async def _file_private_reply(db, workspace_id: str, comment_conv: dict, account: dict, contact: dict,
                              sent: dict, text: str) -> None:
    recipient = sent.get("recipient_id") or contact.get("external_user_id") or ""
    if not (recipient and sent.get("message_id")):
        return
    key = {"workspace_id": workspace_id, "platform": comment_conv["platform"], "kind": Kind.DM.value,
           "external_thread_id": recipient}
    dm = await db[CONVERSATIONS].find_one(key)
    if not dm:
        ident = await db[IDENTITIES].find_one_and_update(
            {"workspace_id": workspace_id, "platform": comment_conv["platform"], "external_user_id": recipient},
            {"$setOnInsert": {"first_seen_at": now(), "display_name": contact.get("display_name", "")}},
            upsert=True, return_document=True)
        await db[CONVERSATIONS].insert_one({
            **key, "channel_account_id": str(account["_id"]), "contact_identity_id": str(ident["_id"]),
            "status": "open", "assignee_id": "", "tags": [], "created_at": now(),
            "last_activity_at": now(), "reply_window_expires_at": None})
        dm = await db[CONVERSATIONS].find_one(key)
    await db[MESSAGES].insert_one({
        "workspace_id": workspace_id, "conversation_id": str(dm["_id"]),
        "provider_message_id": sent["message_id"], "direction": Direction.OUTBOUND.value, "text": text,
        "attachments": [], "parent_id": "", "provider_timestamp": now(), "received_at": now(),
        "delivery": Delivery.ACCEPTED.value, "actor_id": "uri-agent"})


async def _human_handling(db, workspace_id: str, conv: dict) -> bool:
    if conv.get("assignee_id"):
        return True
    if conv.get("kind") != Kind.DM.value:
        return False
    from . import agent
    history = await agent._history(db, workspace_id, str(conv["_id"]))
    return agent._activity(history, now(), conv)[1]


async def run_rules(db, workspace_id: str, conversation_id: str, message_id: str,
                    llm: Optional[Llm] = None, transport=None, private_transport=None) -> Optional[dict]:
    """Answer a stored inbound message from the owner's rules. Returns what was done, or
    None when no rule applies — the caller then lets the AI agent handle it."""
    if not (ObjectId.is_valid(conversation_id) and ObjectId.is_valid(message_id)):
        return None
    conv = await db[CONVERSATIONS].find_one({"_id": ObjectId(conversation_id), "workspace_id": workspace_id})
    msg = await db[MESSAGES].find_one({"_id": ObjectId(message_id)})
    if not conv or not msg or msg.get("direction") != Direction.INBOUND.value or not (msg.get("text") or "").strip():
        return None
    rules = [r for r in await list_rules(db, workspace_id, only_enabled=True)
             if r["kind"] in (("comment", "comment_to_dm") if conv.get("kind") == Kind.COMMENT.value else ("dm",))]
    if not rules:
        return None
    from .accounts import account_for
    account = await account_for(db, conv)
    contact = await db[IDENTITIES].find_one({"_id": ObjectId(conv["contact_identity_id"])}) \
        if ObjectId.is_valid(conv.get("contact_identity_id", "")) else None
    contact = contact or {}
    own = {str((account or {}).get("external_account_id") or ""), str((account or {}).get("page_id") or "")} - {""}
    if not account or contact.get("external_user_id") in own:
        return None        # our own reply coming back must never trigger a rule
    if await _human_handling(db, workspace_id, conv):
        return None

    if llm is None:
        from .agent import openai_json
        llm = openai_json
    rule = await find_rule(rules, conv, msg["text"], llm)
    if not rule or await _cooling_down(db, workspace_id, rule, conv, contact.get("external_user_id", "")):
        return None

    from .agent import AGENT_ACTOR, _first_name
    from .meta_transport import meta_private_reply, meta_transport
    from .send import send_reply
    name = _first_name(contact.get("display_name", ""))
    done: list[str] = []

    if rule["public_reply"] and conv.get("kind") == Kind.COMMENT.value:
        try:
            record = await send_reply(
                db, workspace_id, conversation_id, _fill(rule["public_reply"], name),
                idempotency_key=f"rule:{rule['rule_id']}:reply:{message_id}", actor_id=AGENT_ACTOR,
                transport=transport or meta_transport, account=account, reply_to=msg["provider_message_id"])
            if record.get("delivery") == Delivery.ACCEPTED.value:
                done.append("public_reply")
        except SendRefused:
            pass
    if rule["dm"]:
        text = _fill(rule["dm"], name)
        if conv.get("kind") == Kind.COMMENT.value:
            if await _private_reply(db, workspace_id, rule, conv, account, msg, contact, text,
                                    private_transport or meta_private_reply):
                done.append("dm")
        else:
            try:
                record = await send_reply(
                    db, workspace_id, conversation_id, text,
                    idempotency_key=f"rule:{rule['rule_id']}:dm:{message_id}", actor_id=AGENT_ACTOR,
                    transport=transport or meta_transport, account=account)
                if record.get("delivery") == Delivery.ACCEPTED.value:
                    done.append("dm")
            except SendRefused:
                pass

    if not done:
        return None
    await db[RULE_FIRES].insert_one({
        "workspace_id": workspace_id, "rule_id": rule["rule_id"], "conversation_id": conversation_id,
        "contact": contact.get("external_user_id", ""), "thread": conv.get("external_thread_id"), "at": now()})
    await db[RULES].update_one({"_id": rule["_id"]}, {"$inc": {"fired_count": 1}, "$set": {"last_fired_at": now()}})
    await db[CONVERSATIONS].update_one({"_id": conv["_id"]}, {"$set": {
        "needs_human": False, "handoff_reason": "", "handled_by_rule": {"id": rule["rule_id"], "name": rule["name"], "at": now()}}})
    return {"rule_id": rule["rule_id"], "actions": done}
