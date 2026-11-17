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
    assert mail["to_email"] == "urisocialinsight@gmail.com"
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


def test_waitlist_dedupes_by_email_but_notifies_every_time(env):
    client, db, sent = env
    assert client.post("/public/api-waitlist", json={"email": "A@B.com"}).status_code == 200
    assert client.post("/public/api-waitlist", json={"email": "a@b.com", "name": "Ada"}).status_code == 200

    (row,) = db["api_waitlist"].docs
    assert row["email"] == "a@b.com" and row["name"] == "Ada"
    assert [m["to_email"] for m in sent] == ["urisocialinsight@gmail.com"] * 2
    assert sent[0]["subject"].endswith("New signup") and sent[1]["subject"].endswith("Repeat signup")
    assert all("Name" not in m["html_body"] for m in sent)


def test_waitlist_rejects_bad_email(env):
    client, db, _ = env
    assert client.post("/public/api-waitlist", json={"email": "nope"}).status_code == 422
    assert not db["api_waitlist"].docs


ADS_LEAD = {
    "business": "Mama Put Kitchen",
    "whatsapp": "0801 234 5678",
    "sells": "jollof catering",
    "budget": "₦50,000 – ₦200,000",
    "goal": "More sales",
    "utm_source": "facebook",
    "utm_campaign": "december",
    "page": "/ads",
}


def test_ads_lead_stores_and_emails_precious(env):
    client, db, sent = env
    r = client.post("/public/ads-lead", json=ADS_LEAD)
    assert r.status_code == 200 and r.json()["status"] is True

    (doc,) = db["ads_leads"].docs
    assert doc["business"] == "Mama Put Kitchen" and doc["whatsapp_digits"] == "08012345678"
    assert doc["utm_source"] == "facebook" and doc["status"] == "new"

    (mail,) = sent
    assert mail["to_email"] == "precious.zino@urisocial.com"
    assert "Mama Put Kitchen" in mail["subject"]
    body = mail["html_body"]
    assert "jollof catering" in body and "More sales" in body and "december" in body
    # A Nigerian local number becomes a ready-to-tap WhatsApp link.
    assert "https://wa.me/2348012345678" in body


def test_ads_lead_only_lists_the_utm_tags_it_has(env):
    client, _, sent = env
    client.post("/public/ads-lead", json={k: v for k, v in ADS_LEAD.items() if not k.startswith("utm_")})
    assert "Campaign" not in sent[0]["html_body"] and "Source" not in sent[0]["html_body"]


def test_ads_lead_escapes_html_in_notification(env):
    client, _, sent = env
    client.post("/public/ads-lead", json={**ADS_LEAD, "sells": "<script>alert(1)</script>"})
    assert "<script>" not in sent[0]["html_body"]
    assert "&lt;script&gt;" in sent[0]["html_body"]


def test_ads_lead_subject_header_cannot_be_split(env):
    client, _, sent = env
    client.post("/public/ads-lead", json={**ADS_LEAD, "business": "Bob\r\nBcc: evil@example.com"})
    assert "\r" not in sent[0]["subject"] and "\n" not in sent[0]["subject"]


@pytest.mark.parametrize(
    "patch",
    [
        {"business": ""},
        {"whatsapp": "123"},
        {"whatsapp": "call me maybe"},
        {"whatsapp": "1" * 15},
        {"sells": ""},
        {"budget": ""},
        {"goal": ""},
    ],
)
def test_ads_lead_rejects_bad_input(env, patch):
    client, db, sent = env
    assert client.post("/public/ads-lead", json={**ADS_LEAD, **patch}).status_code == 422
    assert not db["ads_leads"].docs and not sent


def test_ads_lead_honeypot_is_silently_dropped(env):
    client, db, sent = env
    r = client.post("/public/ads-lead", json={**ADS_LEAD, "website": "http://spam"})
    assert r.status_code == 200
    assert not db["ads_leads"].docs and not sent


def test_ads_lead_has_its_own_roomier_rate_limit(env):
    client, _, _ = env
    codes = [client.post("/public/ads-lead", json=ADS_LEAD).status_code for _ in range(mod._ADS_RATE_LIMIT + 1)]
    assert codes == [200] * mod._ADS_RATE_LIMIT + [429]
    # The contact form's own bucket is untouched.
    assert client.post("/public/contact", json=GOOD).status_code == 200
