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

This test scans every Python module under app/ (not just app/services/ —
the same singleton shape could just as easily be written in one of the
app/agents/*/services/ modules) for the specific anti-pattern: assigning
get_db()'s or get_sdk_gateway_db()'s return value — the database handle
itself, or a collection pulled straight off it — onto `self.*`, which is
what makes the value sticky across the process lifetime instead of being
re-read on every access. It fails if this reappears anywhere, including in
a service nobody has written yet at the time this test was added.
get_db()/get_sdk_gateway_db() are cheap, no-I/O lookups; there is no reason
for any caller to cache what they return beyond a single call.
"""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
_LIVE_CALLS = ("get_db", "get_sdk_gateway_db")


def _is_live_call(node: ast.AST) -> bool:
    """`get_db()` / `get_sdk_gateway_db()`, or that call subscripted straight
    into a collection (`get_db()["some_collection"]`) — either shape hands
    back something bound to whatever the client currently is."""
    if isinstance(node, ast.Subscript):
        node = node.value
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _LIVE_CALLS


def _assigns_into_cache_slot(tree: ast.AST) -> list[int]:
    """Line numbers of `self.<X> = <live call>` assignments anywhere in the
    module — the shape that makes a cache slot sticky across the process
    lifetime. A local variable assignment (`db = get_db()`) is fine and
    common; only an assignment onto `self.something` is the dangerous,
    permanent form."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not _is_live_call(node.value):
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
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for lineno in _assigns_into_cache_slot(tree):
            violations.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}")

    assert not violations, (
        "Found code that caches get_db()/get_sdk_gateway_db() (or a "
        "collection pulled straight off one) onto `self.*` — this survives a "
        "DocumentDB password rotation with a dead connection baked in (the "
        "exact cause of a real prod outage). Read get_db()/get_sdk_gateway_db() "
        "fresh on every access instead — they're cheap, no-I/O lookups of the "
        "current client, nothing to gain by caching. Offending line(s):\n  "
        + "\n  ".join(violations)
    )
