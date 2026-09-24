"""
End-to-end reproduction of the real prod incident: a DocumentDB password
rotation while a service singleton is already up and serving, checking
whether it follows the rotation without a process restart.

This replays the exact two-part sequence that happened live:
  1. docdb_credential_refresher._rebuild_client_if_changed() detects the
     password change and swaps the module-level app.database.client — this
     half already worked correctly during the real incident (13 seconds).
  2. A service singleton that had ALREADY resolved `.db` once before the
     rotation — this half was the actual bug: CreditService (and 4 others)
     cached get_db()'s result forever, so they never saw the swap from (1)
     and kept authenticating with the dead password for ~13 hours until the
     whole process was restarted.

No real AWS/DocumentDB calls are made — Secrets Manager and the Mongo
client constructor are both faked, so this runs in CI in well under a
second while still exercising the actual production code paths (not a
reimplementation of them).
"""
import asyncio

import pytest

from app import database as db_module
from app.services import docdb_credential_refresher as refresher_module
from app.services.CreditService import CreditService
from app.services.PaymentService import PaymentService
from app.services.SubscriptionService import SubscriptionService
from app.services.TrialService import TrialService
from app.services.NotificationService import NotificationService

ALL_FIXED_SERVICE_CLASSES = [
    CreditService,
    PaymentService,
    SubscriptionService,
    TrialService,
    NotificationService,
]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeMotorClient:
    """Stand-in for AsyncIOMotorClient — identity is all that matters for
    proving which one a service ends up holding."""

    def get_default_database(self):
        return self


@pytest.fixture
def rotation_scenario(monkeypatch):
    """Wires up a fake password sequence + fake Mongo client constructor,
    and resets the refresher's module-level `_last_known_password` so tests
    don't leak state into each other (it's a plain module global, same as
    the real process's — accurate to how this runs in prod, but needs
    resetting between test runs)."""
    monkeypatch.setattr(refresher_module, "_last_known_password", None)
    monkeypatch.setattr(refresher_module.settings, "DOCDB_SECRET_ARN", "arn:fake:docdb-secret")
    monkeypatch.setattr(refresher_module.settings, "MONGODB_URI", "mongodb://user:old-password@host/db")
    monkeypatch.setattr(refresher_module.settings, "SDK_GATEWAY_MONGODB_URI", None)

    old_client = _FakeMotorClient()
    new_client = _FakeMotorClient()
    monkeypatch.setattr(db_module, "client", old_client)

    passwords = iter(["old-password", "new-password"])

    async def _fake_fetch_current_password():
        return next(passwords)

    monkeypatch.setattr(refresher_module, "_fetch_current_password", _fake_fetch_current_password)
    monkeypatch.setattr("motor.motor_asyncio.AsyncIOMotorClient", lambda uri: new_client)

    return old_client, new_client


@pytest.mark.parametrize("service_cls", ALL_FIXED_SERVICE_CLASSES, ids=lambda c: c.__name__)
def test_service_follows_a_rotation_without_a_restart(rotation_scenario, service_cls):
    """The actual regression test against the current (fixed) code, run
    against all 5 services this incident affected — not just CreditService."""
    old_client, new_client = rotation_scenario

    # App "starts": the service is touched once, same as its real first use
    # shortly after boot.
    service = service_cls()
    assert service.db is old_client, "sanity check: should see whatever client is live at first access"

    # Refresher's own first poll — just confirms the secret is reachable,
    # matches docdb_credential_refresher's documented no-op-on-first-check
    # behavior (connect_to_mongo() already built the initial client).
    _run(refresher_module._rebuild_client_if_changed())
    assert db_module.client is old_client

    # The actual rotation: password changes underneath the running process.
    _run(refresher_module._rebuild_client_if_changed())
    assert db_module.client is new_client, "refresher itself should have swapped the module-level client"

    # THE ASSERTION THAT MATTERS: does the already-running CreditService
    # singleton — created before the rotation, never recreated — now see
    # the new client too? Before the fix this returned old_client forever.
    assert service.db is new_client


def test_the_old_caching_pattern_would_have_failed_this_exact_check(rotation_scenario):
    """Proves the test above actually discriminates bug-present from
    bug-fixed, rather than passing regardless of what CreditService.db does:
    reproduces the exact removed anti-pattern in a throwaway class and runs
    it through the identical rotation sequence. This must fail the same
    assertion the real fix passes — if it didn't, the test above would be
    worthless as a regression guard."""
    old_client, new_client = rotation_scenario

    class _CreditServiceBeforeTheFix:
        """Byte-for-byte the removed pattern from CreditService.db."""

        def __init__(self):
            self._db = None

        @property
        def db(self):
            if self._db is None:
                self._db = db_module.get_db()
            return self._db

    service = _CreditServiceBeforeTheFix()
    assert service.db is old_client

    _run(refresher_module._rebuild_client_if_changed())
    _run(refresher_module._rebuild_client_if_changed())
    assert db_module.client is new_client  # the refresher did its job...

    # ...but the old pattern never sees it — this is the actual outage.
    assert service.db is old_client
    assert service.db is not new_client
