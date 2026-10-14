"""Clearing a conversation removes only that workspace's copy."""


from bson import ObjectId

from app.agents.inbox import agent
from app.agents.inbox.entities import CONVERSATIONS, MESSAGES
from app.agents.inbox.router import delete_conversation
from tests.test_inbox_agent import Db
from tests.test_inbox_ingest import _run


def test_clearing_a_conversation_removes_its_messages_and_drafts_only():
    db = Db()
    cid, other = ObjectId(), ObjectId()
    db[CONVERSATIONS].docs += [{"_id": cid, "workspace_id": "ws"}, {"_id": other, "workspace_id": "ws"}]
    db[MESSAGES].docs += [{"_id": ObjectId(), "workspace_id": "ws", "conversation_id": str(cid)},
                          {"_id": ObjectId(), "workspace_id": "ws", "conversation_id": str(other)}]
    db[agent.CLASSIFICATIONS].docs.append({"_id": ObjectId(), "workspace_id": "ws", "conversation_id": str(cid)})

    _run(delete_conversation(str(cid), db, {"brand_id": "ws"}))

    assert [d["_id"] for d in db[CONVERSATIONS].docs] == [other]
    assert [d["conversation_id"] for d in db[MESSAGES].docs] == [str(other)]
    assert db[agent.CLASSIFICATIONS].docs == []


def test_another_workspace_cannot_clear_it():
    import pytest
    from fastapi import HTTPException
    db = Db()
    cid = ObjectId()
    db[CONVERSATIONS].docs.append({"_id": cid, "workspace_id": "ws"})
    with pytest.raises(HTTPException):
        _run(delete_conversation(str(cid), db, {"brand_id": "ws_other"}))
    assert db[CONVERSATIONS].docs
