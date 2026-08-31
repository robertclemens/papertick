from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import CashFlowKind, Contribution
from app.services import irs


def test_limit_without_catchup(db, user, limits):
    limit, catchup = irs.user_limit(db, user, 2026)
    assert limit == Decimal("7500")
    assert catchup is False


def test_limit_with_catchup(db, user, limits):
    user.date_of_birth = date(1970, 3, 1)  # turns 56 in 2026
    db.commit()
    limit, catchup = irs.user_limit(db, user, 2026)
    assert limit == Decimal("8600")
    assert catchup is True


def test_contribution_within_limit(db, user, roth, limits):
    year, warnings, status = irs.validate_deposit(
        db, user, roth, Decimal("5000"), None, CashFlowKind.CONTRIBUTION,
        today=date(2026, 8, 29),
    )
    assert year == 2026
    assert status.remaining == Decimal("2500")


def test_contribution_exceeding_limit_blocked(db, user, roth, limits):
    db.add(Contribution(account_id=roth.id, tax_year=2026, amount=Decimal("7000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        irs.validate_deposit(db, user, roth, Decimal("1000"), 2026,
                             CashFlowKind.CONTRIBUTION, today=date(2026, 8, 29))
    assert exc.value.status_code == 422
    assert "exceeds" in exc.value.detail


def test_prior_year_designation_window(db, user, roth, limits):
    # Before Tax Day: prior year allowed
    year, _, _ = irs.validate_deposit(
        db, user, roth, Decimal("1000"), 2025, CashFlowKind.CONTRIBUTION,
        today=date(2026, 3, 1),
    )
    assert year == 2025
    # After Tax Day: prior year rejected
    with pytest.raises(HTTPException):
        irs.validate_deposit(db, user, roth, Decimal("1000"), 2025,
                             CashFlowKind.CONTRIBUTION, today=date(2026, 8, 29))


def test_rollover_exempt_from_limits(db, user, roth, limits):
    year, warnings, _ = irs.validate_deposit(
        db, user, roth, Decimal("250000"), None, CashFlowKind.ROLLOVER,
        today=date(2026, 8, 29),
    )
    assert year is None
    assert any("Rollover" in w for w in warnings)


def test_limit_shared_across_ira_accounts(db, user, roth, limits):
    from app.models import Account, AccountType

    trad = Account(user_id=user.id, scenario_id=roth.scenario_id, account_type=AccountType.TRADITIONAL_IRA,
                   name="Trad", settlement_balance=0)
    db.add(trad)
    db.add(Contribution(account_id=roth.id, tax_year=2026, amount=Decimal("7000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    with pytest.raises(HTTPException):
        irs.validate_deposit(db, user, trad, Decimal("1000"), 2026,
                             CashFlowKind.CONTRIBUTION, today=date(2026, 8, 29))
