"""
Shared fixtures for Uri Social API tests.
"""

import pytest
import httpx
from tests.config import BASE_URL, TEST_EMAIL, TEST_PASSWORD, TEST_FIRST, TEST_LAST


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def auth_token(client):
    """Sign up (or log in if already exists) and return a JWT."""
    r = client.post("/auth/signup", json={
        "email": TEST_EMAIL,
        "password": TEST_PASSWORD,
        "first_name": TEST_FIRST,
        "last_name": TEST_LAST,
    })
    if r.status_code == 409:
        r = client.post("/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })
    assert r.status_code == 200, f"Auth failed during test setup: {r.text}"
    return r.json()["responseData"]["accessToken"]


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture(scope="session")
def brand_profile(client, auth_headers):
    """Seed a minimal brand profile for the test user so content generation works."""
    r = client.post(
        "/social-media/brand-profile",
        json={
            "brand_name": "QA Test Brand",
            "industry": "technology",
            "brand_colors": ["#C2185B", "#F5F5F0"],
            "brand_voice": "professional",
            "tagline": "QA test tagline",
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, f"Brand profile setup failed: {r.text}"
    return r.json()
