"""The LinkedIn redirect_uri never goes out as a placeholder."""
from app.agents.social_media_manager.routers import linkedin_router as lr


def test_placeholder_falls_back_to_this_apis_own_callback(monkeypatch):
    monkeypatch.setattr(lr.settings, "LINKEDIN_OAUTH_CALLBACK_URL", "ADD_ME_MANUALLY_IN_CONSOLE", raising=False)
    monkeypatch.setattr(lr.settings, "PUBLIC_API_URL", "https://api-staging.urisocial.com/", raising=False)
    assert lr.linkedin_callback_url() == "https://api-staging.urisocial.com/linkedin/callback"


def test_a_real_configured_url_wins(monkeypatch):
    monkeypatch.setattr(lr.settings, "LINKEDIN_OAUTH_CALLBACK_URL", "https://api.urisocial.com/linkedin/callback", raising=False)
    assert lr.linkedin_callback_url() == "https://api.urisocial.com/linkedin/callback"
