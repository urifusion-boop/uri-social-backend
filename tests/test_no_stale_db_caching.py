"""
Regression guard for the prod outage caused by a DocumentDB password
rotation: several service singletons (CreditService, PaymentService,
SubscriptionService, TrialService, NotificationService) had a `db` property
that cached `get_db()`'s return value on first access and reused it for the
life of the process. `docdb_credential_refresher.py` swaps the module-level
`database.client` the moment DocumentDB rotates the master password, but a
singleton holding its own cached reference never saw the swap — it kept
authenticating with the dead old password until the whole process was
restarted, which is what took content generation down for ~13 hours despite
the refresher itself working correctly within seconds.

This test scans every service module for the specific anti-pattern —
`if self._db is None: self._db = get_db()` (assigning INTO the cache slot
from within the property, rather than only ever reading it) — and fails if
it reappears anywhere, including in a new service nobody wrote yet at the
time this test was added. get_db()/get_sdk_gateway_db() are cheap, no-I/O
lookups; there is no reason for any caller to cache what they return beyond
a single call.
"""
import ast
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parent.parent / "app" / "services"


def _assigns_into_db_cache_slot(tree: ast.AST) -> list[int]:
    """Line numbers of `self.<X> = get_db()` / `self.<X> = get_sdk_gateway_db()`
    assignments anywhere in the module — the one shape that makes a `db`
    property's cache slot sticky across the process lifetime. A local
    variable assignment (`db = get_db()`) is fine and common; only an
    assignment onto `self.something` is the dangerous, permanent form."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in ("get_db", "get_sdk_gateway_db")
        ):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                offenders.append(node.lineno)
    return offenders


def test_no_service_permanently_caches_get_db_result():
    violations = []
    for path in sorted(SERVICES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for lineno in _assigns_into_db_cache_slot(tree):
            violations.append(f"{path.relative_to(SERVICES_DIR.parent.parent)}:{lineno}")

    assert not violations, (
        "Found service code that caches get_db()/get_sdk_gateway_db() onto "
        "`self.*` — this survives a DocumentDB password rotation with a dead "
        "connection baked in (the exact cause of a real prod outage). Read "
        "get_db()/get_sdk_gateway_db() fresh on every access instead — they're "
        "cheap, no-I/O lookups of the current client, nothing to gain by "
        "caching. Offending line(s):\n  " + "\n  ".join(violations)
    )
