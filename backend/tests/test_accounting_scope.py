"""Regressions for figures that were summed across the wrong set of rows.

Two separate bugs put the same kind of wrong number on screen: a report that
spanned every scenario a user owned, and an opening balance booked as a
rollover. Both inflated "IRA contributions" and "rollovers received" without
any single row being wrong on its own.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    Scenario,
)
from app.services import irs
from app.services.tax import tax_report


@pytest.fixture()
def two_tracks(db, user, scenario, roth, limits):
    """The shape that produced the bug: the same history imported twice, into
    two scenarios, as a real user gets by copying a track."""
    other = Scenario(user_id=user.id, name="Copy", sort_order=1)
    db.add(other)
    db.commit()
    twin = Account(user_id=user.id, scenario_id=other.id,
                   account_type=AccountType.ROTH_IRA, name="Roth",
                   settlement_balance=Decimal("0"))
    db.add(twin)
    db.commit()
    for account in (roth, twin):
        db.add(Contribution(account_id=account.id, tax_year=2026,
                            amount=Decimal("5048.09"),
                            kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    return roth, twin


def test_tax_report_counts_one_scenario_not_every_scenario(db, user, two_tracks):
    roth, twin = two_tracks

    report = tax_report(db, user, 2026, roth.scenario_id)
    assert report.ira_contributions == Decimal("5048.09")

    # the other track reports its own copy, not the sum of both
    other = tax_report(db, user, 2026, twin.scenario_id)
    assert other.ira_contributions == Decimal("5048.09")


def test_opening_balance_is_not_reported_as_a_rollover(db, user, scenario, roth, limits):
    db.add(Contribution(account_id=roth.id, tax_year=None, amount=Decimal("223965.68"),
                        kind=CashFlowKind.OPENING_BALANCE,
                        memo="Opening balance copied from Vanguard"))
    db.add(Contribution(account_id=roth.id, tax_year=None, amount=Decimal("1000"),
                        kind=CashFlowKind.ROLLOVER))
    db.commit()

    report = tax_report(db, user, date.today().year, scenario.id)
    assert report.rollovers == Decimal("1000.00")
    assert report.opening_balances == Decimal("223965.68")
    # and it consumes no annual contribution room
    assert irs.contributed_for_year(db, user, date.today().year, scenario.id) == 0


def test_opening_balance_consumes_no_ira_room(db, user, scenario, roth, limits):
    db.add(Contribution(account_id=roth.id, tax_year=None, amount=Decimal("50000"),
                        kind=CashFlowKind.OPENING_BALANCE))
    db.commit()
    buckets = irs.contribution_statuses(db, roth)
    current = next(b for b in buckets if b.tax_year == date.today().year)
    assert current.contributed == 0
    assert current.remaining == current.limit


def test_a_taxable_account_cannot_receive_a_rollover(db, user, taxable, limits):
    with pytest.raises(HTTPException) as exc:
        irs.validate_deposit(db, user, taxable, Decimal("82161.94"), None,
                             CashFlowKind.ROLLOVER)
    assert exc.value.status_code == 422
    assert "cannot receive a rollover" in exc.value.detail


def test_an_opening_balance_cannot_be_deposited(db, user, roth, limits):
    with pytest.raises(HTTPException) as exc:
        irs.validate_deposit(db, user, roth, Decimal("1000"), None,
                             CashFlowKind.OPENING_BALANCE)
    assert exc.value.status_code == 422


def test_a_roth_rollover_is_allowed_but_says_what_it_must_be(db, user, roth, limits):
    _, warnings, _ = irs.validate_deposit(db, user, roth, Decimal("1000"), None,
                                          CashFlowKind.ROLLOVER)
    assert any("conversion" in w for w in warnings)
    assert any("do not count toward annual IRA limits" in w for w in warnings)
