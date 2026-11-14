"""fetch_all_facebook_pages: Pages in a Business portfolio must be offered too
(live report: 4 Pages on the account, only 3 in the picker)."""
import asyncio

from app.agents.social_media_manager.routers.complete_social_manager import fetch_all_facebook_pages

G = "https://graph.facebook.com/v20.0"


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, params=None):
        self.calls.append(url)
        body = self.routes.get(url, {"data": []})
        return _Resp(body(params) if callable(body) else body)


def _page(pid, token=True):
    return {"id": pid, "name": f"P{pid}", **({"access_token": f"tok{pid}"} if token else {})}


def test_business_owned_page_is_included_and_deduped():
    c = _Client({
        f"{G}/me/accounts": {"data": [_page("1"), _page("2"), _page("3")]},
        f"{G}/me/businesses": {"data": [{"id": "b1"}]},
        f"{G}/b1/owned_pages": {"data": [_page("3"), _page("4")]},
    })
    pages = asyncio.run(fetch_all_facebook_pages(c, G, "u"))
    assert sorted(p["id"] for p in pages) == ["1", "2", "3", "4"]


def test_follows_paging_on_me_accounts():
    nxt = f"{G}/me/accounts?after=x"
    c = _Client({
        f"{G}/me/accounts": {"data": [_page("1")], "paging": {"next": nxt}},
        nxt: {"data": [_page("2")]},
    })
    assert sorted(p["id"] for p in asyncio.run(fetch_all_facebook_pages(c, G, "u"))) == ["1", "2"]


def test_business_page_without_token_is_looked_up_then_dropped_if_still_none():
    c = _Client({
        f"{G}/me/accounts": {"data": []},
        f"{G}/me/businesses": {"data": [{"id": "b1"}]},
        f"{G}/b1/client_pages": {"data": [_page("7", token=False), _page("8", token=False)]},
        f"{G}/7": _page("7"),
        f"{G}/8": {"id": "8"},
    })
    assert [p["id"] for p in asyncio.run(fetch_all_facebook_pages(c, G, "u"))] == ["7"]


def test_missing_business_permission_falls_back_to_me_accounts():
    c = _Client({
        f"{G}/me/accounts": {"data": [_page("1")]},
        f"{G}/me/businesses": {"error": {"message": "(#100) Missing Permission"}},
    })
    assert [p["id"] for p in asyncio.run(fetch_all_facebook_pages(c, G, "u"))] == ["1"]
