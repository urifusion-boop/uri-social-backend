"""Unless the client names a neighbourhood, Jane targets the whole state or city."""
import asyncio
import json
from types import SimpleNamespace

from app.agents.jane_ads import jane_consultant as jc

READY = {
    "stage": "ready", "business_name": "Uri", "category": "Marketing", "goal": "messages",
    "offer_type": "service", "budget_ngn": 50000, "city": "Lagos", "geo_mode": "watering_hole",
    "geo_areas": [{"name": "Victoria Island", "reason": "hub"}, {"name": "Central Business District", "reason": "hub"}],
    "stated_plan": "I'll target Victoria Island and the CBD.",
}


def _run(monkeypatch, message, data=READY):
    seen: list[str] = []

    class _Completions:
        async def create(self, **kw):
            seen.append(kw["messages"][-1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))])

    class _Client:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(jc.openai, "AsyncOpenAI", _Client)
    monkeypatch.setattr(jc, "settings", SimpleNamespace(jane_ads_openai_key="test"))
    return asyncio.run(jc.consult(message, "Uri", "Marketing", history=[])), seen


def test_states_only_means_the_whole_states(monkeypatch):
    brief, seen = _run(monkeypatch, "Webinar for business owners in Lagos, Abuja and Ibadan. 50,000")
    assert brief.whole_area and brief.city == "Lagos, Abuja, Ibadan" and brief.geo_areas == []
    assert "ALL of Lagos, Abuja, Ibadan" in seen[0]


def test_no_place_named_covers_the_whole_city_jane_picked(monkeypatch):
    brief, _ = _run(monkeypatch, "Webinar for business owners. 50,000")
    assert brief.whole_area and brief.city == "Lagos" and brief.geo_areas == []


def test_a_named_neighbourhood_keeps_the_pockets(monkeypatch):
    brief, _ = _run(monkeypatch, "Webinar for business owners in Victoria Island, Lagos. 50,000")
    assert not brief.whole_area and brief.geo_areas


def test_named_neighbourhoods_are_the_only_areas(monkeypatch):
    brief, _ = _run(monkeypatch, "Promote Uri to small business owners in Lekki and Ikeja. 40,000")
    assert [a["name"] for a in brief.geo_areas] == ["Lekki", "Ikeja"]
    assert "Victoria Island" not in brief.stated_plan


def test_whole_area_message_never_names_pockets(monkeypatch):
    data = {**READY, "stated_plan": "I'll cover all of Lagos, focusing on areas like Lekki and Ikeja."}
    brief, _ = _run(monkeypatch, "Promote Uri to small business owners in Lagos. 40,000", data)
    assert brief.stated_plan == "I'll cover all of Lagos."
