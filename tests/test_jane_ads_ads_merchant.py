"""Ad money settles into its OWN Squad merchant, not Uri's subscription account.

Ad top-ups are client money Uri holds to spend on their behalf; subscriptions are Uri's
own revenue. Sharing one merchant makes the two indistinguishable at the bank and in
Squad's dashboard, and there is no way to unpick it afterwards.
"""
import asyncio

import pytest

from app.agents.jane_ads.payments import _ads_squad_credentials


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


SHARED = {"secret_key": "sk_subs", "public_key": "pk_subs",
          "api_url": "https://api-d.squadco.com", "mode": "live"}


def _patch_shared(monkeypatch, creds=None):
    async def _shared():
        return dict(creds or SHARED)
    monkeypatch.setattr("app.agents.jane_ads.payments.payment_service._get_squad_credentials", _shared)


def test_the_ads_merchant_is_used_when_configured(monkeypatch):
    from app.core.config import settings

    _patch_shared(monkeypatch)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_SECRET_KEY", "sk_ads", raising=False)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_PUBLIC_KEY", "pk_ads", raising=False)
    creds = _run(_ads_squad_credentials())
    assert creds["secret_key"] == "sk_ads"
    assert creds["public_key"] == "pk_ads"
    assert creds["api_url"] == SHARED["api_url"]   # same Squad, different merchant


def test_an_unconfigured_ads_merchant_changes_nothing(monkeypatch):
    """Every environment that has never set these must behave exactly as before —
    adding the setting cannot be what breaks an existing deployment."""
    from app.core.config import settings

    _patch_shared(monkeypatch)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_SECRET_KEY", None, raising=False)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_PUBLIC_KEY", None, raising=False)
    assert _run(_ads_squad_credentials())["secret_key"] == "sk_subs"


def test_half_configured_falls_back_rather_than_mixing_merchants(monkeypatch):
    """A secret from one merchant with a public key from another authenticates as
    neither. Fall back whole rather than send a mismatched pair."""
    from app.core.config import settings

    _patch_shared(monkeypatch)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_SECRET_KEY", "sk_ads", raising=False)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_PUBLIC_KEY", None, raising=False)
    assert _run(_ads_squad_credentials())["secret_key"] == "sk_subs"


def test_sandbox_is_never_redirected(monkeypatch):
    """Test money has nowhere to be separated to, and the ads merchant has no sandbox
    pair — sending sandbox traffic a live key would just fail."""
    from app.core.config import settings

    _patch_shared(monkeypatch, {**SHARED, "mode": "sandbox", "secret_key": "sk_sandbox"})
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_SECRET_KEY", "sk_ads", raising=False)
    monkeypatch.setattr(settings, "SQUAD_ADS_LIVE_PUBLIC_KEY", "pk_ads", raising=False)
    assert _run(_ads_squad_credentials())["secret_key"] == "sk_sandbox"
