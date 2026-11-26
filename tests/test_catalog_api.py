"""Catalog API end to end: roles, tenant isolation, the shop-floor flow, CSV, reports and the AI's limits."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.agents.catalog import indexes
from app.agents.catalog.router import router
from app.dependencies import get_active_brand_context, get_db_dependency

ME = {"user_id": "u1", "brand_id": "b1", "agency_id": None}


@pytest.fixture()
def env():
    db = AsyncMongoMockClient()["t"]
    indexes.reset()
    app = FastAPI()
    app.include_router(router)
    who = dict(ME)
    app.dependency_overrides[get_db_dependency] = lambda: db
    app.dependency_overrides[get_active_brand_context] = lambda: who
    with TestClient(app) as client:
        client.who, client.db = who, db
        yield client
    indexes.reset()


def shoe(client, **over):
    body = {"name": "Air Max", "description": "Running shoe", "item_type": "physical", "category": "footwear",
            "price": 45000, "initial_quantity": 10, "cost_price": 20000, "sku": "AM-1"}
    body.update(over)
    res = client.post("/catalog/items", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def test_shop_flow_create_reserve_pay_and_history(env):
    item = shoe(env)
    assert item["stock"]["available"] == 10 and item["cost_price"] == 20000
    res = env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 3, "idempotency_key": "o-1"})
    assert res.status_code == 201 and res.json()["status"] == "pending"
    chk = env.get("/catalog/availability/check", params={"item_id": item["id"], "quantity": 8}).json()
    assert chk["available"] is False and chk["available_quantity"] == 7
    assert env.post(f"/catalog/reservations/{res.json()['id']}/confirm").json()["status"] == "confirmed"
    moves = env.get("/catalog/inventory/movements", params={"item_id": item["id"]}).json()["movements"]
    assert {m["movement_type"] for m in moves} == {"received", "reserved", "sold"}
    assert env.get("/catalog/inventory/reconcile", params={"item_id": item["id"]}).json()["matches"]


def test_validation_errors_are_readable(env):
    res = env.post("/catalog/items", json={"name": "x", "item_type": "physical"})
    assert res.status_code == 400 and "required" in res.json()["detail"].lower()
    item = shoe(env)
    over = env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 99})
    assert over.status_code == 409 and "available" in over.json()["detail"].lower()
    assert env.get("/catalog/items/nope").status_code == 404


def test_staff_can_read_and_reserve_but_not_change_prices_stock_or_see_cost(env):
    item = shoe(env)
    env.who.update(agency_id="ag1", user_id="u2")
    from app.services import AgencyService as svc
    original = svc.AgencyService.is_agency_admin

    async def not_admin(*a, **k):
        return False
    svc.AgencyService.is_agency_admin = staticmethod(not_admin)
    try:
        assert env.get(f"/catalog/items/{item['id']}").json().get("cost_price") is None
        assert env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 1}).status_code == 201
        assert env.patch(f"/catalog/items/{item['id']}", json={"price": 1}).status_code == 403
        assert env.post("/catalog/inventory/adjustments", json={"item_id": item["id"], "movement_type": "received",
                                                                "quantity": 5}).status_code == 403
        assert env.post("/catalog/items", json={}).status_code == 403
    finally:
        svc.AgencyService.is_agency_admin = original


def test_one_business_never_sees_anothers_catalog(env):
    item = shoe(env)
    env.who["brand_id"] = "b2"
    assert env.get(f"/catalog/items/{item['id']}").status_code == 404
    assert env.get("/catalog/items").json()["total"] == 0
    assert env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 1}).status_code == 404
    assert env.get("/catalog/ai/search", params={"q": "air max"}).json()["items"] == []


def test_stock_adjustments_and_threshold_alerts(env):
    item = shoe(env, initial_quantity=5, low_stock_threshold=3)
    env.post("/catalog/inventory/adjustments", json={"item_id": item["id"], "movement_type": "damaged", "quantity": 3,
                                                     "reason": "water"}).raise_for_status()
    alerts = env.get("/catalog/alerts").json()["alerts"]
    assert [a["type"] for a in alerts] == ["low_stock"]
    count = env.post("/catalog/inventory/adjustments", json={"item_id": item["id"], "set_to": 0, "reason": "stock take"})
    assert count.json()["available"] == 0
    assert env.get("/catalog/alerts").json()["alerts"][0]["type"] == "out_of_stock"
    bad = env.post("/catalog/inventory/adjustments", json={"item_id": item["id"], "movement_type": "lost", "quantity": 1,
                                                           "reason": "gone"})
    assert bad.status_code == 409


def test_variant_flow_and_sessions(env):
    item = shoe(env, initial_quantity=None)
    v = env.post(f"/catalog/items/{item['id']}/variants",
                 json={"name": "Black / 42", "attributes": {"size": "42"}, "initial_quantity": 2}).json()
    assert env.get(f"/catalog/items/{item['id']}").json()["variants"][0]["stock"]["available"] == 2
    assert env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 1}).status_code == 400
    assert env.post("/catalog/reservations", json={"item_id": item["id"], "variant_id": v["id"], "quantity": 1}).status_code == 201

    tour = shoe(env, name="Zanzibar", item_type="capacity", availability_type="capacity", sku="ZN", initial_quantity=None)
    slot = env.post(f"/catalog/items/{tour['id']}/slots", json={"start_at": "2099-12-10T08:00:00Z",
                                                                 "end_at": "2099-12-15T08:00:00Z", "total": 2,
                                                                 "label": "December"})
    assert slot.status_code == 201
    book = {"item_id": tour["id"], "quantity": 2, "start_at": "2099-12-10T08:00:00Z"}
    assert env.post("/catalog/reservations", json=book).status_code == 201
    assert env.post("/catalog/reservations", json={**book, "quantity": 1}).status_code == 409
    cal = env.get("/catalog/availability/calendar", params={"item_id": tour["id"], "start": "2099-12-01T00:00:00Z",
                                                            "end": "2099-12-31T00:00:00Z"}).json()
    assert cal["slots"][0]["remaining"] == 0 and cal["slots"][0]["bookable"] is False
    assert env.patch(f"/catalog/slots/{slot.json()['id']}", json={"total": 1}).status_code == 409


def test_csv_import_export_roundtrip_with_errors_and_dry_run(env):
    csv_text = ("Name,Description,Type,Category,SKU,Price,Stock,Variant,Variant Stock\n"
                "Air Max,Running shoe,product,Footwear,AM,\"₦45,000\",,Black / 42,3\n"
                ",,,,,,,Black / 43,0\n"
                "Hoodie,Warm hoodie,product,Clothing,HD,12000,7,,\n"
                "Broken,,product,,,abc,,,\n")
    dry = env.post("/catalog/import", json={"csv": csv_text, "dry_run": True}).json()
    assert dry["created"] == 2 and len(dry["errors"]) == 1 and env.get("/catalog/items").json()["total"] == 0
    real = env.post("/catalog/import", json={"csv": csv_text}).json()
    assert real["created"] == 2 and real["errors"][0]["name"] == "Broken"
    listing = {i["name"]: i for i in env.get("/catalog/items").json()["items"]}
    assert listing["Hoodie"]["available_quantity"] == 7 and listing["Air Max"]["variant_count"] == 2
    assert listing["Air Max"]["price"] == 45000
    again = env.post("/catalog/import", json={"csv": csv_text.replace("Hoodie,Warm hoodie,product,Clothing,HD,12000,7",
                                                                      "Hoodie,Warm hoodie,product,Clothing,HD,13000,9")}).json()
    assert again["updated"] == 2 and again["created"] == 0
    hoodie = next(i for i in env.get("/catalog/items").json()["items"] if i["name"] == "Hoodie")
    assert hoodie["price"] == 13000 and hoodie["available_quantity"] == 9
    exported = env.get("/catalog/export")
    assert exported.status_code == 200 and "Air Max" in exported.text and "Black / 43" in exported.text


def test_reports_count_sales_conversion_and_missed_demand(env):
    item = shoe(env, initial_quantity=2)
    r = env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 2}).json()
    env.post(f"/catalog/reservations/{r['id']}/confirm")
    from app.agents.catalog import demand
    client = env
    client.portal.call(demand.record, env.db, "b1", item["id"], None, "out_of_stock", "do you have air max", "onebox", "m1")
    out = env.get("/catalog/reports/summary").json()
    assert out["sold"][0]["units"] == 2 and out["sold"][0]["remaining"] == 0
    assert out["reservations"]["conversion_rate"] == 1.0
    assert out["demand_without_availability"][0]["enquiries"] == 1
    assert "not lost revenue" in out["note"]


def test_the_ai_can_read_check_and_reserve_after_confirmation_but_nothing_more(env):
    item = shoe(env, initial_quantity=3)
    found = env.get("/catalog/ai/search", params={"q": "do you have the air max?"}).json()["items"]
    assert found[0]["id"] == item["id"] and "cost_price" not in found[0] and "sales_notes" not in found[0]
    assert "45,000" in found[0]["display_price"]
    draft = env.post("/catalog/ai/draft-orders", json={"lines": [{"item_id": item["id"], "quantity": 2, "unit_price": 1}]}).json()
    assert draft["total"] == 90000, "prices come from the catalog, never from the caller"
    assert env.get("/catalog/availability/check", params={"item_id": item["id"]}).json()["available_quantity"] == 3
    assert env.post(f"/catalog/ai/draft-orders/{draft['id']}/reserve", json={}).status_code == 400
    done = env.post(f"/catalog/ai/draft-orders/{draft['id']}/reserve", json={"customer_confirmed": True}).json()
    assert done["status"] == "reserved"
    assert env.get("/catalog/availability/check", params={"item_id": item["id"]}).json()["available_quantity"] == 1
    too_many = env.post("/catalog/ai/draft-orders", json={"lines": [{"item_id": item["id"], "quantity": 5}]}).json()
    assert env.post(f"/catalog/ai/draft-orders/{too_many['id']}/reserve", json={"customer_confirmed": True}).status_code == 409
    assert env.get("/catalog/availability/check", params={"item_id": item["id"]}).json()["available_quantity"] == 1
    # There is no AI route that creates items, changes prices or adds stock.
    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes if r.path.startswith("/catalog/ai")}
    assert all(m != "PATCH" and m != "DELETE" for _, methods in paths for m in methods)
    assert not any("adjust" in p or p.endswith("/items") for p, _ in paths)
    assert env.post(f"/catalog/ai/draft-orders/{draft['id']}/release").json()["status"] == "cancelled"
    assert env.get("/catalog/availability/check", params={"item_id": item["id"]}).json()["available_quantity"] == 3


def test_changing_availability_with_open_reservations_is_refused(env):
    item = shoe(env)
    env.post("/catalog/reservations", json={"item_id": item["id"], "quantity": 1})
    res = env.patch(f"/catalog/items/{item['id']}", json={"availability_type": "unlimited"})
    assert res.status_code == 409 and "open reservations" in res.json()["detail"]
    assert env.patch(f"/catalog/items/{item['id']}", json={"price": 50000}).json()["price"] == 50000
