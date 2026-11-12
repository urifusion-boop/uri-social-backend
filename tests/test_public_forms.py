"""
Public marketing-site forms — app/routers/public_forms_router.py
(Contact page "Send message", API Docs page "Join the API waitlist").
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_db_dependency
from app.routers import public_forms_router as mod


class FakeResult:
    def __init__(self, upserted_id=None):
        self.upserted_id = upserted_id


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return FakeResult(None)
        if not upsert:
            return FakeResult(None)
        doc = dict(update.get("$setOnInsert", {}))
        doc.update(update.get("$set", {}))
        self.docs.append(doc)
        return FakeResult(upserted_id=len(self.docs))


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, FakeCollection())


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(mod.email_service, "send_raw_email", fake_send)
    mod._hits.clear()

    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[get_db_dependency] = lambda: db
    return TestClient(app), db, sent


GOOD = {"name": "Precious", "email": "Zinop96@Gmail.com", "subject": "General Inquiry", "message": "hello"}


def test_contact_stores_and_emails_hello(env):
    client, db, sent = env
    r = client.post("/public/contact", json=GOOD)
    assert r.status_code == 200 and r.json()["status"] is True

    (doc,) = db["contact_messages"].docs
    assert doc["email"] == "zinop96@gmail.com" and doc["message"] == "hello"

    (mail,) = sent
    assert mail["to_email"] == "hello@urisocial.com"
    assert mail["reply_to"].lower() == "zinop96@gmail.com"
    assert "General Inquiry" in mail["subject"]


def test_contact_escapes_html_in_notification(env):
    client, _, sent = env
    client.post("/public/contact", json={**GOOD, "message": "<script>alert(1)</script>"})
    assert "<script>" not in sent[0]["html_body"]
    assert "&lt;script&gt;" in sent[0]["html_body"]


def test_contact_subject_header_cannot_be_split(env):
    client, _, sent = env
    client.post("/public/contact", json={**GOOD, "name": "Bob\r\nBcc: evil@example.com"})
    assert "\r" not in sent[0]["subject"] and "\n" not in sent[0]["subject"]


@pytest.mark.parametrize(
    "patch",
    [{"email": "not-an-email"}, {"message": ""}, {"name": ""}, {"subject": "Free money"}],
)
def test_contact_rejects_bad_input(env, patch):
    client, db, sent = env
    assert client.post("/public/contact", json={**GOOD, **patch}).status_code == 422
    assert not db["contact_messages"].docs and not sent


def test_contact_honeypot_is_silently_dropped(env):
    client, db, sent = env
    r = client.post("/public/contact", json={**GOOD, "website": "http://spam"})
    assert r.status_code == 200
    assert not db["contact_messages"].docs and not sent


def test_contact_is_rate_limited_per_ip(env):
    client, _, _ = env
    codes = [client.post("/public/contact", json=GOOD).status_code for _ in range(mod._RATE_LIMIT + 1)]
    assert codes == [200] * mod._RATE_LIMIT + [429]


def test_waitlist_dedupes_by_email_and_only_notifies_first_time(env):
    client, db, sent = env
    assert client.post("/public/api-waitlist", json={"email": "A@B.com"}).status_code == 200
    assert client.post("/public/api-waitlist", json={"email": "a@b.com", "name": "Ada"}).status_code == 200

    (row,) = db["api_waitlist"].docs
    assert row["email"] == "a@b.com" and row["name"] == "Ada"
    assert len(sent) == 1 and sent[0]["to_email"] == "hello@urisocial.com"


def test_waitlist_rejects_bad_email(env):
    client, db, _ = env
    assert client.post("/public/api-waitlist", json={"email": "nope"}).status_code == 422
    assert not db["api_waitlist"].docs
