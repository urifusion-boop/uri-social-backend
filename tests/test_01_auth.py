"""
Tests: Authentication
─────────────────────
✅ Sign up with email
✅ Duplicate signup rejected
✅ Login with email
✅ Login with wrong password rejected
✅ Login with unknown email rejected
"""

import uuid
import pytest
import httpx
from tests.conftest import BASE_URL, TEST_EMAIL, TEST_PASSWORD, TEST_FIRST, TEST_LAST


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


class TestSignup:
    def test_signup_success(self, client):
        """New user can sign up successfully."""
        email = f"qa+signup+{uuid.uuid4().hex[:6]}@urisocial.com"
        r = client.post("/auth/signup", json={
            "email": email,
            "password": "TestPass123!",
            "first_name": "Test",
            "last_name": "User",
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        assert "accessToken" in data["responseData"]
        assert data["responseData"]["email"] == email

    def test_signup_duplicate_email(self, client):
        """Signing up with an existing email returns 409."""
        r = client.post("/auth/signup", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "first_name": TEST_FIRST,
            "last_name": TEST_LAST,
        })
        # First call may succeed (test user created by conftest) — try again
        r2 = client.post("/auth/signup", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "first_name": TEST_FIRST,
            "last_name": TEST_LAST,
        })
        assert r2.status_code == 409, f"Expected 409, got {r2.status_code}: {r2.text}"

    def test_signup_missing_email(self, client):
        """Signup without email returns 422."""
        r = client.post("/auth/signup", json={
            "password": "TestPass123!",
            "first_name": "No",
            "last_name": "Email",
        })
        assert r.status_code == 422

    def test_signup_trial_activated(self, client):
        """Signup response includes trial info."""
        email = f"qa+trial+{uuid.uuid4().hex[:6]}@urisocial.com"
        r = client.post("/auth/signup", json={
            "email": email,
            "password": "TestPass123!",
            "first_name": "Trial",
            "last_name": "User",
        })
        assert r.status_code == 200, r.text
        trial = r.json()["responseData"].get("trial")
        assert trial is not None, "Trial info missing from signup response"
        assert trial.get("is_trial") is True


class TestLogin:
    def test_login_success(self, client):
        """Existing user can log in."""
        r = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        assert "accessToken" in data["responseData"]

    def test_login_wrong_password(self, client):
        """Wrong password returns 401."""
        r = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": "WrongPassword!",
        })
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"

    def test_login_unknown_email(self, client):
        """Unknown email returns 401."""
        r = client.post("/auth/login", json={
            "email": "nobody@nonexistent.com",
            "password": "TestPass123!",
        })
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"

    def test_login_returns_user_fields(self, client):
        """Login response includes expected fields."""
        r = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })
        assert r.status_code == 200
        data = r.json()["responseData"]
        for field in ("accessToken", "userId", "email", "firstName"):
            assert field in data, f"Missing field: {field}"
