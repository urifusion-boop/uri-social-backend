"""
Website → Brand Playbook (PRD Workstream 1) and the durable job queue it runs on.

Covers: the queue (idempotent enqueue, one claim per job, lease takeover, retry then fail),
link safety (private addresses, metadata IP, odd ports, credentials, robots.txt), page reading,
what the extraction keeps, comparing findings with the Playbook, applying only chosen fields
(lists added to, never replaced), and the whole job end to end against fake websites.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.agents.brand_intel import extractor, service
from app.agents.brand_intel.html_read import pick_pages, read_page, top_colors
from app.agents.brand_intel.safe_fetch import (
    FetchError, fetch, normalize_url, resolve_public, robots_permits, site_key,
)
from app.jobs import queue, worker


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    return AsyncMongoMockClient()["test_brand_intel"]


# ── the job queue ───────────────────────────────────────────────────────────────

def test_asking_twice_for_the_same_read_returns_the_same_job():
    db = _db()
    _run(queue.ensure_indexes(db))
    a = _run(queue.enqueue(db, "website_read", {"url": "u"}, brand_id="b1", key="b1:magen.ng"))
    b = _run(queue.enqueue(db, "website_read", {"url": "u"}, brand_id="b1", key="b1:magen.ng"))
    assert a["job_id"] == b["job_id"]


def test_a_finished_job_frees_its_key_for_a_new_one():
    db = _db()
    _run(queue.ensure_indexes(db))
    a = _run(queue.enqueue(db, "website_read", {}, key="k"))
    job = _run(queue.claim(db, ["website_read"]))
    _run(queue.complete(db, job["job_id"], {"ok": True}))
    b = _run(queue.enqueue(db, "website_read", {}, key="k"))
    assert a["job_id"] != b["job_id"]


def test_a_job_is_claimed_once():
    db = _db()
    _run(queue.enqueue(db, "website_read", {}))
    first = _run(queue.claim(db, ["website_read"]))
    second = _run(queue.claim(db, ["website_read"]))
    assert first and first["status"] == "running" and first["attempts"] == 1
    assert second is None


def test_a_job_whose_worker_vanished_is_taken_over_after_its_lease():
    db = _db()
    _run(queue.enqueue(db, "website_read", {}))
    first = _run(queue.claim(db, ["website_read"]))
    _run(db[queue.COLLECTION].update_one({"job_id": first["job_id"]},
                                         {"$set": {"lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}}))
    again = _run(queue.claim(db, ["website_read"]))
    assert again["job_id"] == first["job_id"] and again["attempts"] == 2


def test_a_failure_is_retried_later_then_fails_with_a_readable_message():
    db = _db()
    _run(queue.enqueue(db, "t", {}, max_attempts=2))
    job = _run(queue.claim(db, ["t"]))
    _run(queue.fail(db, job, "Try again"))
    doc = _run(queue.get(db, job["job_id"]))
    assert doc["status"] == "queued" and doc["run_at"].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    _run(db[queue.COLLECTION].update_one({"job_id": job["job_id"]}, {"$set": {"run_at": datetime.now(timezone.utc)}}))
    job = _run(queue.claim(db, ["t"]))
    _run(queue.fail(db, job, "Your website took too long."))
    doc = _run(queue.get(db, job["job_id"]))
    assert doc["status"] == "failed" and doc["error"]["message"] == "Your website took too long."
    assert queue.public_view(doc)["error"]["message"] == "Your website took too long."


def test_the_worker_runs_a_handler_and_stores_its_result_and_steps():
    db = _db()

    async def handler(db_, job, ctx):
        await ctx.step("home", "Reading your home page")
        await ctx.step("home", "Reading your home page", "done")
        await ctx.found(["Airport runs"])
        return {"answer": 42}

    worker.register("unit_test_job", handler)
    _run(queue.enqueue(db, "unit_test_job", {}))
    job = _run(queue.claim(db, ["unit_test_job"]))
    _run(worker.run_one(db, job))
    view = queue.public_view(_run(queue.get(db, job["job_id"])))
    assert view["status"] == "done"
    assert view["steps"] == [{"id": "home", "label": "Reading your home page", "status": "done", "detail": "", "order": 1}]
    assert view["found"] == ["Airport runs"]


def test_a_handler_refusal_fails_without_retrying():
    db = _db()

    async def handler(db_, job, ctx):
        raise worker.JobError("Your website asks tools not to read it, so we didn't.", code="robots")

    worker.register("unit_refuse", handler)
    _run(queue.enqueue(db, "unit_refuse", {}, max_attempts=3))
    job = _run(queue.claim(db, ["unit_refuse"]))
    _run(worker.run_one(db, job))
    doc = _run(queue.get(db, job["job_id"]))
    assert doc["status"] == "failed" and doc["error"]["code"] == "robots"


# ── link safety ────────────────────────────────────────────────────────────────

def test_addresses_are_normalised_or_refused_in_plain_words():
    assert normalize_url("magen.ng") == "https://magen.ng/"
    assert normalize_url(" HTTP://Magen.NG/about ") == "http://magen.ng/about"
    for bad, code in [("", "empty"), ("ftp://x.com", "scheme"), ("https://a:b@x.com", "credentials"),
                      ("https://x.com:8080", "port"), ("magen", "host")]:
        with pytest.raises(FetchError) as e:
            normalize_url(bad)
        assert e.value.code == code


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.1", "::1",
                                  "localhost", "db.internal", "0.0.0.0"])
def test_private_and_internal_addresses_are_refused(host):
    with pytest.raises(FetchError) as e:
        _run(resolve_public(host))
    assert e.value.code == "private"


def test_a_name_that_resolves_to_a_private_address_is_refused():
    async def fake_getaddrinfo(*a, **k):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]

    with patch.object(asyncio.get_event_loop(), "getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(FetchError) as e:
            _run(resolve_public("evil.example"))
    assert e.value.code == "private"


def test_a_redirect_into_a_private_address_is_refused():
    def handler(request: httpx.Request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    with patch("app.agents.brand_intel.safe_fetch.resolve_public", new=AsyncMock(side_effect=_resolve_or_block)):
        with pytest.raises(FetchError) as e:
            _run(fetch("https://magen.ng/", client=client))
    assert e.value.code == "private"


async def _resolve_or_block(host):
    if host == "169.254.169.254":
        raise FetchError("That address isn't a public website.", "private")
    return "93.184.216.34"


def test_robots_txt_is_respected_for_our_bot_and_for_everyone():
    txt = "User-agent: *\nDisallow: /admin\n\nUser-agent: UriBot\nDisallow: /\nAllow: /about"
    assert robots_permits(txt, "/about")
    assert not robots_permits(txt, "/services")
    assert not robots_permits("User-agent: *\nDisallow: /", "/")
    assert robots_permits("User-agent: *\nDisallow:", "/anything")
    assert robots_permits("", "/")


def test_site_key_ignores_www():
    assert site_key("https://www.Magen.ng/about") == "magen.ng"


# ── reading a page ─────────────────────────────────────────────────────────────

HOME = """<!doctype html><html lang="en"><head><title>Magen | Airport &amp; staff transport</title>
<meta name="description" content="Reliable airport, staff and event transport in Lagos.">
<meta property="og:site_name" content="Magen"><meta name="theme-color" content="#0E4D92">
<style>:root{--color-primary:#C2185B}.btn{background:#F4A300}.x{color:#eeeeee}.err{color:#ef4444}.e2{color:#ef4444}.e3{color:#ef4444}</style>
<script type="application/ld+json">{"@type":"LocalBusiness","name":"Magen Transport","priceRange":"₦25,000 - ₦80,000","areaServed":"Lagos"}</script>
<script>var secret = "do not read me";</script></head>
<body><nav><a href="/about-us">About us</a><a href="/services">Our services</a><a href="/pricing">Pricing</a>
<a href="/contact">Contact</a><a href="/terms">Terms of service</a><a href="/privacy-policy">Privacy</a><a href="https://instagram.com/magen.ng">IG</a><a href="https://wa.me/2348031234567">WhatsApp</a>
<a href="https://other.com/about">Elsewhere</a><a href="/brochure.pdf">Brochure</a></nav>
<h1>Never miss a flight again</h1><p>Airport runs from ₦25,000.</p>
<img src="/img/logo.png" alt="Magen logo"></body></html>"""


def test_a_page_is_read_into_the_parts_that_matter():
    r = read_page(HOME, "https://magen.ng/")
    assert r.title == "Magen | Airport & staff transport"
    assert r.description.startswith("Reliable airport")
    assert r.og["site_name"] == "Magen" and r.theme_color == "#0E4D92"
    assert "Never miss a flight again" in r.headings
    assert "Airport runs from ₦25,000." in r.text and "do not read me" not in r.text
    assert r.socials["instagram"] == "https://instagram.com/magen.ng"
    assert r.socials["whatsapp"].startswith("https://wa.me/")
    assert r.jsonld[0]["priceRange"] == "₦25,000 - ₦80,000"
    assert r.logo_candidates[0] == "https://magen.ng/img/logo.png"


def test_the_right_pages_are_picked_from_the_same_site_only_and_legal_pages_skipped():
    picks = dict(pick_pages(read_page(HOME, "https://magen.ng/")))
    assert picks == {"about": "https://magen.ng/about-us", "services": "https://magen.ng/services",
                     "pricing": "https://magen.ng/pricing", "contact": "https://magen.ng/contact"}


def test_brand_colours_put_the_theme_first_and_skip_greys():
    r = read_page(HOME, "https://magen.ng/")
    assert top_colors(r.colors, r.theme_color) == ["#0e4d92", "#c2185b", "#f4a300"]


# ── what extraction keeps ──────────────────────────────────────────────────────

SITE = {"url": "https://magen.ng/", "pages": ["https://magen.ng/", "https://magen.ng/services"],
        "text": "Magen. Airport runs from ₦25,000. \"Always on time\" - Tola A. Serving Lekki and VI."}


def test_invented_testimonials_and_prices_are_dropped_and_real_ones_kept():
    raw = {
        "brand_name": {"value": "Magen", "basis": "stated", "page": "https://magen.ng/"},
        "proof_points": {"value": ["\"Always on time\" - Tola A.", "Best service in Lagos - Chidi"], "basis": "stated"},
        "price_range": {"value": "₦10,000 – ₦15,000", "basis": "stated"},
        "tagline": {"value": "Moving Lagos forward", "basis": "stated"},
        "key_products_services": {"value": "Airport runs (from ₦25,000)", "basis": "stated", "page": "https://evil.com"},
        "industry": {"value": "", "basis": "inferred"},
        "made_up_field": {"value": "x"},
    }
    out = extractor.clean(raw, SITE)
    assert out["proof_points"]["value"] == ["\"Always on time\" - Tola A."]
    assert "price_range" not in out and "tagline" not in out and "industry" not in out
    assert "made_up_field" not in out
    assert out["key_products_services"]["value"] == ["Airport runs (from ₦25,000)"]
    assert out["key_products_services"]["page"] == "https://magen.ng/"   # unknown page → the site


def test_a_price_level_without_numbers_is_kept_as_a_guess():
    out = extractor.clean({"price_range": {"value": "premium", "basis": "stated"}}, SITE)
    assert out["price_range"] == {"value": "premium", "basis": "inferred", "page": "https://magen.ng/"}


def test_extraction_falls_back_to_the_next_model_when_one_is_unavailable():
    import openai
    calls = []

    async def create(**kw):
        calls.append(kw["model"])
        if kw["model"] == extractor.MODELS[0]:
            raise openai.NotFoundError("no such model", response=httpx.Response(404, request=httpx.Request("POST", "http://x")), body=None)
        from tests.test_jane_ads_plan_defence import _mock_openai_response
        return _mock_openai_response({"fields": {"brand_name": {"value": "Magen", "basis": "stated"}}})

    with patch("openai.AsyncOpenAI") as cls, patch.object(extractor.settings, "OPENAI_API_KEY", "k"):
        cls.return_value.chat.completions.create = AsyncMock(side_effect=create)
        out = _run(extractor.extract(SITE))
    assert calls[:2] == list(extractor.MODELS[:2])
    assert out["brand_name"]["value"] == "Magen"


# ── comparing and applying ─────────────────────────────────────────────────────

FIELDS = {
    "brand_name": {"value": "Magen", "basis": "stated", "page": "p"},
    "product_description": {"value": "Airport, staff and event transport in Lagos.", "basis": "inferred", "page": "p"},
    "key_products_services": {"value": ["Airport runs", "Staff buses"], "basis": "stated", "page": "p"},
    "customer_pain_points": {"value": ["Missing flights"], "basis": "inferred", "page": "p"},
    "social_links": {"value": {"instagram": "https://instagram.com/magen.ng"}, "basis": "stated", "page": "p"},
}
PROFILE = {"brand_name": "magen ", "product_description": "We make moving easier",
           "key_products_services": ["Airport runs", "Hourly hire"], "customer_pain_points": []}


def test_findings_are_lined_up_against_the_playbook_and_only_new_ones_preselected():
    rows = {r["key"]: r for r in service.compare(FIELDS, PROFILE)}
    assert rows["brand_name"]["status"] == "same"
    assert rows["product_description"]["status"] == "different" and not rows["product_description"]["selected"]
    assert rows["key_products_services"]["status"] == "different"
    assert rows["customer_pain_points"]["status"] == "new" and rows["customer_pain_points"]["selected"]
    assert rows["product_description"]["label"] == "Description"
    assert rows["social_links"]["label"] == "Social links"


def test_applying_adds_to_lists_replaces_text_and_records_the_source():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    upd = service.build_update(FIELDS, ["key_products_services", "product_description", "social_links"],
                               {**PROFILE, "social_links": {"facebook": "https://facebook.com/magen"}}, now=now)
    assert upd["key_products_services"] == ["Airport runs", "Hourly hire", "Staff buses"]
    assert upd["product_description"] == "Airport, staff and event transport in Lagos."
    assert upd["social_links"] == {"instagram": "https://instagram.com/magen.ng", "facebook": "https://facebook.com/magen"}
    assert upd["field_sources"]["product_description"]["source"] == "website"
    assert upd["field_sources"]["product_description"]["basis"] == "inferred"
    assert "brand_name" not in upd


def test_an_owner_edit_before_applying_is_theirs_not_the_websites():
    upd = service.build_update(FIELDS, ["product_description"], PROFILE, edits={"product_description": "Airport transport"})
    assert upd["product_description"] == "Airport transport"
    assert upd["field_sources"]["product_description"]["source"] == "you"


def test_a_field_reads_from_the_website_until_the_owner_changes_it():
    upd = service.build_update(FIELDS, ["product_description"], PROFILE)
    profile = {**PROFILE, **upd}
    assert service.source_of(profile, "product_description") == "guess"
    profile["product_description"] = "My own words"
    assert service.source_of(profile, "product_description") == "you"
    assert service.source_of(profile, "brand_name") == "you"


# ── the whole job, end to end ──────────────────────────────────────────────────

class _Ctx:
    def __init__(self):
        self.steps, self.found_items = [], []

    async def step(self, sid, label, status="running", detail=""):
        self.steps.append((sid, status))

    async def found(self, items):
        self.found_items += items


def _site_transport(pages: dict):
    def handler(request: httpx.Request):
        path = request.url.path
        if path in pages:
            body, ctype = pages[path]
            return httpx.Response(200, text=body, headers={"content-type": ctype})
        return httpx.Response(404, text="nope", headers={"content-type": "text/html"})
    return httpx.MockTransport(handler)


def _patched_client(transport):
    real = httpx.AsyncClient

    def make(*a, **k):
        k.pop("transport", None)
        return real(*a, transport=transport, **k)
    return make


def test_a_site_is_read_extracted_cached_and_reused():
    db = _db()
    pages = {"/robots.txt": ("User-agent: *\nDisallow: /pricing", "text/plain"),
             "/": (HOME, "text/html; charset=utf-8"),
             "/about-us": ("<h1>About Magen</h1><p>Since 2019 we move Lagos.</p>", "text/html"),
             "/services": ("<h2>Airport runs</h2><h2>Staff buses</h2>", "text/html"),
             "/contact": ("<p>Lekki Phase 1</p>", "text/html")}
    fake_fields = {"brand_name": {"value": "Magen", "basis": "stated", "page": "https://magen.ng/"},
                   "key_products_services": {"value": ["Airport runs", "Staff buses"], "basis": "stated", "page": "https://magen.ng/services"}}
    extract = AsyncMock(return_value=dict(fake_fields))
    with patch("app.agents.brand_intel.service.httpx.AsyncClient", new=_patched_client(_site_transport(pages))), \
         patch("app.agents.brand_intel.safe_fetch.resolve_public", new=AsyncMock(return_value="93.184.216.34")), \
         patch.object(extractor, "extract", new=extract):
        ctx = _Ctx()
        job = {"job_id": "j1", "payload": {"url": "https://magen.ng/"}}
        result = _run(service.read_site_job(db, job, ctx))
        site_text = extract.await_args.args[0]["text"]
        again = _run(service.read_site_job(db, {"job_id": "j2", "payload": {"url": "https://www.magen.ng/"}}, _Ctx()))

    assert result["site"] == "magen.ng"
    assert result["fields"]["brand_name"]["value"] == "Magen"
    assert result["fields"]["website"]["value"] == "https://magen.ng/"
    assert result["fields"]["social_links"]["value"]["instagram"] == "https://instagram.com/magen.ng"
    assert result["fields"]["brand_colors"]["value"][0] == "#0e4d92"
    assert "Since 2019 we move Lagos." in site_text and "Lekki Phase 1" in site_text
    assert "/pricing" not in site_text            # robots.txt said no
    assert "STRUCTURED BUSINESS DATA" in site_text
    assert ("understand", "done") in ctx.steps and "Airport runs" in ctx.found_items
    assert extract.await_count == 1               # the second read came from the cache
    assert again["fields"]["brand_name"]["value"] == "Magen"


def test_a_site_that_forbids_reading_fails_without_retrying():
    db = _db()
    pages = {"/robots.txt": ("User-agent: *\nDisallow: /", "text/plain"), "/": (HOME, "text/html")}
    with patch("app.agents.brand_intel.service.httpx.AsyncClient", new=_patched_client(_site_transport(pages))), \
         patch("app.agents.brand_intel.safe_fetch.resolve_public", new=AsyncMock(return_value="93.184.216.34")):
        with pytest.raises(worker.JobError) as e:
            _run(service.read_site_job(db, {"job_id": "j", "payload": {"url": "https://magen.ng/"}}, _Ctx()))
    assert e.value.code == "robots" and e.value.retry is False


def test_a_site_that_blocks_tools_says_so():
    db = _db()
    with patch("app.agents.brand_intel.service.httpx.AsyncClient",
               new=_patched_client(httpx.MockTransport(lambda r: httpx.Response(403, text="no")))), \
         patch("app.agents.brand_intel.safe_fetch.resolve_public", new=AsyncMock(return_value="93.184.216.34")):
        with pytest.raises(worker.JobError) as e:
            _run(service.read_site_job(db, {"job_id": "j", "payload": {"url": "https://magen.ng/"}}, _Ctx()))
    assert e.value.message == "Your website doesn't let tools read it." and e.value.retry is False


def test_a_brand_cannot_start_unlimited_reads():
    db = _db()
    for i in range(service.READS_PER_BRAND_PER_DAY):
        _run(queue.enqueue(db, service.JOB_TYPE, {}, brand_id="b1"))
    with pytest.raises(FetchError) as e:
        _run(service.start_read(db, brand_id="b1", user_id="u", url="magen.ng"))
    assert e.value.code == "rate_limited"


def test_the_owner_gets_a_bell_notification_when_a_read_finishes():
    db = _db()
    pages = {"/": (HOME, "text/html")}
    told = AsyncMock()
    with patch("app.agents.brand_intel.service.httpx.AsyncClient", new=_patched_client(_site_transport(pages))), \
         patch("app.agents.brand_intel.safe_fetch.resolve_public", new=AsyncMock(return_value="93.184.216.34")), \
         patch.object(extractor, "extract", new=AsyncMock(return_value={})), \
         patch("app.services.brand_notifier.notify_in_app", new=told):
        _run(service.read_site_job(db, {"job_id": "j9", "user_id": "u1", "brand_id": "b1",
                                        "payload": {"url": "https://magen.ng/"}}, _Ctx()))
    args = told.await_args
    assert args.args[1:4] == ("u1", "b1", "playbook_ready")
    assert args.kwargs["title"] == "Your Playbook is ready to check"
    assert "magen.ng" in args.kwargs["message"]
