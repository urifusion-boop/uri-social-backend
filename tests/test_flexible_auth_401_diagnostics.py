"""
flexible_auth's two 401 paths now log enough to diagnose a recurrence
without needing the user to reproduce it again.

Live-reported: a Facebook/Instagram reconnect click silently bounced the
user with zero visible error — traced to a 401 from flexible_auth
(confirmed via CloudWatch, NOT a plain expired-token 403 — this codebase's
JWTBearer/decode_jwt always returns 403 for that). flexible_auth only ever
401s for two reasons: no Authorization/X-API-Key header at all, or a JWT
that decoded fine but has no userId/user_id claim. Both are now logged
(path, claim keys or header presence, user-agent — never the token itself)
so the exact cause is visible in CloudWatch the next time it happens,
instead of having to ask the customer to try again.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.dependencies import flexible_auth


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeRequest:
    def __init__(self, headers: dict, path: str = "/social-media/connect/initiate"):
        self.headers = headers
        self.url = type("URL", (), {"path": path})()


def test_missing_auth_header_still_401s_and_logs_diagnostics(capsys):
    req = FakeRequest(headers={})
    with pytest.raises(HTTPException) as exc_info:
        _run(flexible_auth(request=req, x_api_key=None, x_end_user_id=None, db=None))
    assert exc_info.value.status_code == 401

    out = capsys.readouterr().out
    assert "no Authorization/X-API-Key header" in out
    assert "/social-media/connect/initiate" in out
    assert "has_auth_header=False" in out


def test_malformed_auth_header_prefix_is_logged_not_the_token(capsys):
    req = FakeRequest(headers={"authorization": "NotBearer somesecrettoken"})
    with pytest.raises(HTTPException) as exc_info:
        _run(flexible_auth(request=req, x_api_key=None, x_end_user_id=None, db=None))
    assert exc_info.value.status_code == 401

    out = capsys.readouterr().out
    assert "somesecrettoken" not in out  # never log the credential itself
    assert "NotBearer" in out  # just enough of the prefix to tell the shape apart


def test_jwt_missing_user_id_claim_401s_and_logs_claim_keys(capsys, monkeypatch):
    from app.core.auth_bearer import JWTBearer

    async def fake_call(self, request):
        return {"claims": {"email": "someone@example.com"}}  # no userId/user_id

    monkeypatch.setattr(JWTBearer, "__call__", fake_call)

    req = FakeRequest(headers={"authorization": "Bearer sometoken"})
    with pytest.raises(HTTPException) as exc_info:
        _run(flexible_auth(request=req, x_api_key=None, x_end_user_id=None, db=None))
    assert exc_info.value.status_code == 401
    assert "User ID not found" in exc_info.value.detail

    out = capsys.readouterr().out
    assert "no userId/user_id in claims" in out
    assert "'email'" in out  # shows what WAS present, to see the actual shape


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
