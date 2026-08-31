from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException, Response

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    CostBasisMethod,
    OrderSide,
    OrderStatus,
    QuantityType,
    User,
)
from app import security
from app.schemas import OrderCreateIn, ProfileUpdateIn, SignupIn
from app.services import trading
from app.models import OrderSource


@pytest.fixture()
def prod_settings(monkeypatch):
    from app.config import get_settings

    s = get_settings().model_copy(update={"env": "production"})
    for target in ("app.routers.auth.get_settings",):
        monkeypatch.setattr(target, lambda: s)
    return s


def test_signup_requires_email_verification_in_production(db, prod_settings):
    from app.routers.auth import login, signup, verify_email
    from app.schemas import EmailTokenIn, LoginIn

    resp = Response()
    out = signup(SignupIn(email="new.user@example.com", password="a-strong-pass-123",
                          date_of_birth=date(1990, 1, 1)), resp, db)
    assert out.verification_required is True
    assert out.tokens is None
    user = db.query(User).filter_by(email="new.user@example.com").one()
    assert user.email_verified is False

    with pytest.raises(HTTPException) as exc:
        login(LoginIn(email="new.user@example.com", password="a-strong-pass-123"), Response(), db)
    assert exc.value.status_code == 403 and "not verified" in exc.value.detail

    token = security.make_email_verify_token(user.id)
    result = verify_email(EmailTokenIn(token=token), db)
    assert result["status"] == "verified"
    out2 = login(LoginIn(email="new.user@example.com", password="a-strong-pass-123"), Response(), db)
    assert out2.tokens is not None


def test_signup_skips_verification_in_development(db):
    from app.routers.auth import signup

    out = signup(SignupIn(email="dev.user@example.com", password="a-strong-pass-123",
                          date_of_birth=date(1990, 1, 1)), Response(), db)
    assert out.verification_required is False and out.tokens is not None
    assert db.query(User).filter_by(email="dev.user@example.com").one().email_verified is True


# ---------------------------------------------------------------- profile

def _principal(user):
    from app.deps import Principal, SESSION_SCOPES

    return Principal(user=user, scopes=set(SESSION_SCOPES))


def test_dob_change_flags_catchup_over_contribution(db, user, roth, limits):
    # born 1970 -> catch-up eligible; contribute the full catch-up limit for 2026
    user.date_of_birth = date(1970, 3, 1)
    user.password_hash = security.hash_password("a-strong-pass-123")
    db.add(Contribution(account_id=roth.id, tax_year=2026, amount=Decimal("8600"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    from app.routers.auth import dob_impact, update_profile

    impact = dob_impact(date(1990, 6, 1), _principal(user), db)
    assert any("EXCEED" in w for w in impact.warnings)

    with pytest.raises(HTTPException) as exc:
        update_profile(ProfileUpdateIn(date_of_birth=date(1990, 6, 1)), _principal(user), db)
    assert exc.value.status_code == 409

    out = update_profile(
        ProfileUpdateIn(date_of_birth=date(1990, 6, 1), confirm_impacts=True), _principal(user), db
    )
    assert out.user.date_of_birth == date(1990, 6, 1)
    assert out.warnings


def test_email_change_requires_password_and_applies_in_dev(db, user):
    user.password_hash = security.hash_password("a-strong-pass-123")
    db.commit()
    from app.routers.auth import update_profile

    with pytest.raises(HTTPException) as exc:
        update_profile(ProfileUpdateIn(email="fresh@example.com"), _principal(user), db)
    assert exc.value.status_code == 401

    out = update_profile(
        ProfileUpdateIn(email="fresh@example.com", current_password="a-strong-pass-123"),
        _principal(user), db,
    )
    assert out.email_change == "applied"
    assert out.user.email == "fresh@example.com"


# ---------------------------------------------------------------- cost basis restrictions

def test_ira_sell_rejects_cost_basis_election(db, user, roth, taxable):
    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, roth,
            OrderCreateIn(account_id=roth.id, ticker="VOO", side=OrderSide.SELL,
                          quantity_type=QuantityType.SHARES, quantity=Decimal("1"),
                          cost_basis_method=CostBasisMethod.HIFO),
            OrderSource.API,
        )
    assert exc.value.status_code == 422
    assert "taxable brokerage" in exc.value.detail


def test_ira_cost_basis_endpoint_rejected(db, user, roth):
    from app.routers.accounts import set_cost_basis
    from app.schemas import CostBasisUpdateIn

    with pytest.raises(HTTPException) as exc:
        set_cost_basis(roth.id, CostBasisUpdateIn(method=CostBasisMethod.HIFO),
                       _principal(user), db)
    assert exc.value.status_code == 422


def test_ira_sale_records_fifo(db, user, roth, taxable):
    roth.settlement_balance = Decimal("10000")
    db.commit()
    _, buy = trading.place_order(
        db, roth,
        OrderCreateIn(account_id=roth.id, ticker="VOO", side=OrderSide.BUY,
                      quantity_type=QuantityType.DOLLARS, quantity=Decimal("1000"),
                      as_of=date.today() - timedelta(days=100)),
        OrderSource.API,
    )
    order, sell = trading.place_order(
        db, roth,
        OrderCreateIn(account_id=roth.id, ticker="VOO", side=OrderSide.SELL,
                      quantity_type=QuantityType.SHARES, quantity=buy.shares_filled),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    assert order.cost_basis_method == CostBasisMethod.FIFO


def test_specid_without_lots_falls_back_to_fifo(db, user, taxable):
    """Vanguard behavior: SpecID with no shares named at sale time uses FIFO."""
    from app.routers.accounts import set_cost_basis
    from app.schemas import CostBasisUpdateIn

    set_cost_basis(taxable.id, CostBasisUpdateIn(method=CostBasisMethod.SPEC_ID),
                   _principal(user), db)
    for days in (400, 30):
        trading.place_order(
            db, taxable,
            OrderCreateIn(account_id=taxable.id, ticker="VOO", side=OrderSide.BUY,
                          quantity_type=QuantityType.DOLLARS, quantity=Decimal("1000"),
                          as_of=date.today() - timedelta(days=days)),
            OrderSource.API,
        )
    order, sell = trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side=OrderSide.SELL,
                      quantity_type=QuantityType.SHARES, quantity=Decimal("1")),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    assert order.cost_basis_method == CostBasisMethod.FIFO   # recorded honestly
    assert Decimal(sell.realized_lt) != 0 and Decimal(sell.realized_st) == 0  # oldest lot first


def test_average_lock_flag_after_sale(db, user, taxable, fund_asset):
    from app.routers.accounts import get_cost_basis, set_cost_basis
    from app.schemas import CostBasisUpdateIn

    set_cost_basis(taxable.id, CostBasisUpdateIn(method=CostBasisMethod.AVERAGE, ticker="VFIAX"),
                   _principal(user), db)
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VFIAX", side=OrderSide.BUY,
                      quantity_type=QuantityType.DOLLARS, quantity=Decimal("1000"),
                      as_of=date.today() - timedelta(days=50)),
        OrderSource.API,
    )
    cfg = get_cost_basis(taxable.id, _principal(user), db)
    assert cfg.overrides[0].average_locked is False  # revocable before first sale
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VFIAX", side=OrderSide.SELL,
                      quantity_type=QuantityType.SHARES, quantity=Decimal("1")),
        OrderSource.API,
    )
    cfg = get_cost_basis(taxable.id, _principal(user), db)
    assert cfg.overrides[0].average_locked is True  # locked after first averaged sale


# ---------------------------------------------------------------- passkeys

def test_passkey_registration_options(db, user, monkeypatch):
    from app.services import passkeys

    store: dict[str, bytes] = {}
    monkeypatch.setattr(passkeys, "_store_challenge", lambda k, c: store.__setitem__(k, c))
    opts = passkeys.registration_options(db, user)
    assert opts["rp"]["id"] == "localhost"
    assert opts["authenticatorSelection"]["residentKey"] == "required"
    assert opts["challenge"]
    assert f"reg:{user.id}" in store


def test_passkey_auth_options(monkeypatch):
    from app.services import passkeys

    store: dict[str, bytes] = {}
    monkeypatch.setattr(passkeys, "_store_challenge", lambda k, c: store.__setitem__(k, c))
    flow_id, opts = passkeys.authentication_options()
    assert opts["challenge"]
    assert opts.get("allowCredentials", []) == []  # discoverable / usernameless
    assert f"auth:{flow_id}" in store

def test_password_login_asks_for_mfa_but_a_passkey_never_does(db, user, monkeypatch):
    """The staged sign-in relies on this split: password + MFA is three steps,
    a passkey is one. A passkey is already possession plus a biometric/PIN, so
    adding a code on top would be a third factor, not a second."""
    from fastapi import Response

    from app.routers.auth import login
    from app.routers.passkeys_router import login_verify
    from app.schemas import LoginIn, PasskeyLoginVerifyIn
    from app.services import passkeys

    user.password_hash = security.hash_password("a-strong-pass-123")
    user.mfa_secret_enc = security.seal_totp_secret(security.new_totp_secret())
    user.mfa_enabled = True
    db.commit()

    out = login(LoginIn(email=user.email, password="a-strong-pass-123"), Response(), db)
    assert out.mfa_required is True and out.mfa_token and out.tokens is None

    monkeypatch.setattr(passkeys, "verify_authentication", lambda db_, flow, cred: user)
    passkey_out = login_verify(
        PasskeyLoginVerifyIn(flow_id="flow", credential={}), Response(), db
    )
    assert passkey_out.mfa_required is False
    assert passkey_out.tokens is not None      # signed in outright


# ---------------------------------------------------------------- password change

def test_password_change_requires_current_and_revokes_sessions(db, user):
    from app.models import RefreshToken
    from app.routers.auth import change_password
    from app.schemas import PasswordChangeIn

    user.password_hash = security.hash_password("a-strong-pass-123")
    db.add(RefreshToken(
        user_id=user.id, token_hash="x" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    ))
    db.commit()

    with pytest.raises(HTTPException) as exc:
        change_password(
            PasswordChangeIn(current_password="wrong-one", new_password="another-strong-1"),
            Response(), _principal(user), db,
        )
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:  # too weak
        change_password(
            PasswordChangeIn(current_password="a-strong-pass-123", new_password="short"),
            Response(), _principal(user), db,
        )
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:  # unchanged
        change_password(
            PasswordChangeIn(current_password="a-strong-pass-123",
                             new_password="a-strong-pass-123"),
            Response(), _principal(user), db,
        )
    assert exc.value.status_code == 422

    change_password(
        PasswordChangeIn(current_password="a-strong-pass-123",
                         new_password="a-different-pass-456"),
        Response(), _principal(user), db,
    )
    assert security.verify_password("a-different-pass-456", user.password_hash)
    # the old session token is gone; a fresh one was issued for this session
    stale = db.query(RefreshToken).filter(RefreshToken.token_hash == "x" * 64).one()
    assert stale.revoked_at is not None


def test_contribution_status_is_per_account_and_shared(db, user, roth, taxable, limits):
    from datetime import date as _date

    from app.models import CashFlowKind, Contribution
    from app.services.trading import account_out

    year = _date.today().year
    db.add(Contribution(account_id=roth.id, tax_year=year, amount=Decimal("3000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()

    statuses = account_out(db, roth).contribution_statuses
    assert len(statuses) == 1  # prior year is closed (past Tax Day) in this fixture
    status = statuses[0]
    assert status.tax_year == year and status.is_prior_year is False
    assert status.contributed == Decimal("3000")
    assert status.contributed_here == Decimal("3000")
    assert status.remaining == status.limit - Decimal("3000")
    assert 0 < status.used_pct < 100
    # a taxable brokerage has no annual limit
    assert account_out(db, taxable).contribution_statuses == []


def test_prior_year_bucket_stays_open_until_tax_day(db, user, roth, limits):
    """Between Jan 1 and Tax Day both years are fundable, and an undesignated
    contribution defaults to the prior year so its room is used before it
    lapses."""
    from datetime import date as _date

    from app.models import CashFlowKind, Contribution, IrsLimit
    from app.services import irs

    db.add(IrsLimit(tax_year=2027, ira_limit=Decimal("7500"), ira_catchup=Decimal("1100"),
                    designation_deadline=_date(2028, 4, 17)))
    db.add(Contribution(account_id=roth.id, tax_year=2026, amount=Decimal("4000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()

    in_window = _date(2027, 3, 1)          # 2027 tax year, before Tax Day 2027
    buckets = irs.open_tax_years(db, user, in_window)
    assert [b[0] for b in buckets] == [2026, 2027]   # prior year first
    assert irs.default_tax_year(db, user, in_window) == 2026

    statuses = irs.contribution_statuses(db, roth, in_window)
    assert [s.tax_year for s in statuses] == [2027, 2026]
    prior = statuses[1]
    assert prior.is_prior_year and prior.remaining == prior.limit - Decimal("4000")
    assert statuses[0].is_prior_year is False

    # an undesignated deposit lands in the prior-year bucket
    year, _warn, _status = irs.validate_deposit(
        db, user, roth, Decimal("100"), None, CashFlowKind.CONTRIBUTION, today=in_window
    )
    assert year == 2026

    # once that room is exhausted the prior-year bar disappears and the default moves on
    db.add(Contribution(account_id=roth.id, tax_year=2026, amount=prior.remaining,
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    assert all(not s.is_prior_year for s in irs.contribution_statuses(db, roth, in_window))
    assert irs.default_tax_year(db, user, in_window) == 2027


def test_prior_year_bucket_closes_after_tax_day(db, user, roth, limits):
    from datetime import date as _date

    from app.services import irs

    after = _date(2026, 8, 1)  # past Tax Day 2026, so tax year 2025 has lapsed
    assert [b[0] for b in irs.open_tax_years(db, user, after)] == [2026]
    assert all(not s.is_prior_year for s in irs.contribution_statuses(db, roth, after))


def test_rollover_ira_takes_rollovers_only(db, user, roth, limits):
    """A Rollover IRA holds rollover money: regular contributions commingle it
    and forfeit rolling into a future employer plan, so it has no contribution
    bucket, no limit bar, and no external funding."""
    from app.models import Account, AccountType, CashFlowKind
    from app.services import irs
    from app.services.trading import fundable_amount

    rollover = Account(user_id=user.id, scenario_id=roth.scenario_id, account_type=AccountType.ROLLOVER_IRA,
                       name="Rollover", settlement_balance=Decimal("0"),
                       allow_external_funding=True)
    db.add(rollover)
    db.commit()

    assert irs.contribution_statuses(db, rollover) == []
    assert fundable_amount(db, rollover) == Decimal("0")

    with pytest.raises(HTTPException) as exc:
        irs.validate_deposit(db, user, rollover, Decimal("1000"), None,
                             CashFlowKind.CONTRIBUTION)
    assert exc.value.status_code == 422
    assert "commingles" in exc.value.detail

    # a rollover deposit is fine and never touches the annual limit
    year, warnings, status = irs.validate_deposit(
        db, user, rollover, Decimal("50000"), None, CashFlowKind.ROLLOVER
    )
    assert year is None and status is None
    assert any("do not count toward annual IRA limits" in w for w in warnings)


def test_rollover_balance_does_not_consume_the_ira_limit(db, user, roth, limits):
    """The shared annual limit spans Roth and Traditional only."""
    from datetime import date as _date

    from app.models import Account, AccountType, CashFlowKind, Contribution
    from app.services import irs

    rollover = Account(user_id=user.id, scenario_id=roth.scenario_id, account_type=AccountType.ROLLOVER_IRA,
                       name="Rollover", settlement_balance=Decimal("0"))
    db.add(rollover)
    db.commit()
    year = _date.today().year
    db.add(Contribution(account_id=rollover.id, tax_year=year, amount=Decimal("6000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()

    assert irs.contributed_for_year(db, user, year) == Decimal("0")
    status = irs.contribution_statuses(db, roth)[0]
    assert status.contributed == Decimal("0")
    assert status.remaining == status.limit
