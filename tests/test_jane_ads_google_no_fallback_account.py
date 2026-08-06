"""
CI guard (non-negotiable product rule): no platform-wide default/fallback Google Ads
account env var may ever be introduced. This is deliberately a SOURCE-TEXT grep, not a
check against `settings` values — a config-based check (e.g. "assert
settings.GOOGLE_ADS_DEFAULT_CUSTOMER_ID is None") could be silently defeated by a
future settings refactor that renames or restructures fields; grepping the actual
source text can't be quietly bypassed that way.

Mirrors, in spirit, why Meta's own META_ADS_PAGE_ID fallback (ads_connection.py) is a
known, accepted flaw we are NOT fixing in this phase — this test exists so Google
never repeats it. A Google Ads account is 1:1 per brand (created under, or linked to,
URI's MCC via google_ads_connection.py); there is never a legitimate "default" account
to fall back to the way Meta's shared Page was.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"

# Matches GOOGLE_ADS_DEFAULT_*, GOOGLE_ADS_SHARED_*, GOOGLE_ADS_FALLBACK_* — any
# settings-field-shaped name signalling a platform-wide/shared Google Ads account, the
# exact shape META_ADS_PAGE_ID has. GOOGLE_ADS_MCC_CUSTOMER_ID is explicitly EXEMPT: it
# is the agency-level Manager Account id (never a substitute for a brand's own
# customer_id — see google_ads_connection.resolve_customer_id_for_launch, which never
# returns it as a customer_id, only as login_customer_id).
_FORBIDDEN_PATTERN = re.compile(r"\bGOOGLE_ADS_(DEFAULT|SHARED|FALLBACK)_[A-Z_]*")


def test_no_default_fallback_google_ads_account_setting_exists():
    offenders = []
    for py_file in APP_ROOT.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        if _FORBIDDEN_PATTERN.search(text):
            offenders.append(str(py_file.relative_to(REPO_ROOT)))
    assert not offenders, (
        "Found a forbidden platform-wide default/fallback Google Ads account setting "
        f"referenced in: {offenders}. Google Ads customer_id must always resolve "
        "per-brand via google_ads_connection.resolve_customer_id_for_launch() — no "
        "shared/default account id is allowed (explicit product decision, unlike "
        "Meta's accepted META_ADS_PAGE_ID fallback)."
    )


def test_resolve_customer_id_for_launch_has_no_trailing_fallback_return():
    """A narrower, code-shaped companion check: resolve_customer_id_for_launch's own
    source must never contain a pattern like ads_connection.resolve_ads_page_for_launch's
    `if not settings.META_ADS_PAGE_ID: ... return {"page_id": settings.META_ADS_PAGE_ID`
    — i.e. every code path must end in either `raise` or a `return` built from a real
    connection doc, never one built straight from a settings value."""
    conn_file = APP_ROOT / "agents" / "jane_ads" / "google_ads_connection.py"
    text = conn_file.read_text(encoding="utf-8")
    start = text.index("async def resolve_customer_id_for_launch")
    rest = text[start:]
    next_def = rest.find("\nasync def ", 1)
    body = rest[:next_def] if next_def != -1 else rest
    # login_customer_id legitimately falls back to the MCC setting (that's the manager
    # side of the link, not a substitute customer_id) — every OTHER settings reference
    # would be the forbidden shape.
    other_settings_refs = [
        line for line in body.splitlines()
        if "settings." in line and "GOOGLE_ADS_MCC_CUSTOMER_ID" not in line
    ]
    assert not other_settings_refs, (
        f"resolve_customer_id_for_launch references settings directly outside the "
        f"login_customer_id fallback — this looks like a reintroduced account "
        f"fallback: {other_settings_refs}"
    )
