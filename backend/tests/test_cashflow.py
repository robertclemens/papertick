"""Deposits and withdrawals against the cash ledger."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import OptionPosition, OptionRight, PositionSide
from app.schemas import WithdrawIn


def _principal(user):
    from app.deps import SESSION_SCOPES, Principal

    return Principal(user=user, scopes=set(SESSION_SCOPES))


def test_withdrawal_preserves_collateral(db, user, taxable):
    """Regression: a withdrawal must debit only the requested amount. It once
    wrote back the collateral-reduced balance, silently destroying the
    collateral held for short puts."""
    from app.routers.accounts import withdraw

    taxable.settlement_balance = Decimal("14900.00")
    db.add(OptionPosition(
        account_id=taxable.id, underlying="VOO", right=OptionRight.PUT,
        strike=Decimal("34"), expiry=date(2026, 12, 18), side=PositionSide.SHORT,
        contracts=1, avg_premium=Decimal("0.50"), collateral=Decimal("3400.00"),
        opened_on=date.today(),
    ))
    db.commit()

    result = withdraw(taxable.id, WithdrawIn(amount=Decimal("5973.50")), _principal(user), db)
    assert Decimal(result.account.settlement_balance) == Decimal("8926.50")
    assert result.account.buying_power == Decimal("5526.50")  # minus the $3,400 collateral


def test_withdrawal_cannot_touch_collateral(db, user, taxable):
    from app.routers.accounts import withdraw

    taxable.settlement_balance = Decimal("5000.00")
    db.add(OptionPosition(
        account_id=taxable.id, underlying="VOO", right=OptionRight.PUT,
        strike=Decimal("40"), expiry=date(2026, 12, 18), side=PositionSide.SHORT,
        contracts=1, avg_premium=Decimal("0.50"), collateral=Decimal("4000.00"),
        opened_on=date.today(),
    ))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        withdraw(taxable.id, WithdrawIn(amount=Decimal("2000")), _principal(user), db)
    assert exc.value.status_code == 422
    assert "collateral" in exc.value.detail
    assert Decimal(taxable.settlement_balance) == Decimal("5000.00")  # nothing moved


def test_plain_withdrawal_debits_exactly(db, user, taxable):
    from app.routers.accounts import withdraw

    taxable.settlement_balance = Decimal("1000.00")
    db.commit()
    result = withdraw(taxable.id, WithdrawIn(amount=Decimal("250.25")), _principal(user), db)
    assert Decimal(result.account.settlement_balance) == Decimal("749.75")
    assert Decimal(result.contribution.amount) == Decimal("-250.25")
