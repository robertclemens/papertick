"""Buying power, committed cash, and external-bank auto-funding."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    CashFlowKind,
    Contribution,
    OrderSide,
    OrderSource,
    OrderStatus,
    QuantityType,
)
from app.schemas import OrderCreateIn
from app.services import trading

SATURDAY = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)  # market closed


def _buy(account_id, amount="1000", **kw):
    base = dict(account_id=account_id, ticker="VOO", side=OrderSide.BUY,
                order_type="MARKET", quantity_type=QuantityType.DOLLARS,
                quantity=Decimal(amount))
    base.update(kw)
    return OrderCreateIn(**base)


# ---------------------------------------------------------------- committed cash

def test_queued_order_commits_its_cash(db, taxable, enforce_hours):
    """The reported bug: a queued order must earmark its cash so a second
    order cannot spend the same dollars."""
    taxable.settlement_balance = Decimal("8600")
    taxable.allow_external_funding = False
    db.commit()

    order1, txn1 = trading.place_order(
        db, taxable, _buy(taxable.id, "8600"), OrderSource.API, now=SATURDAY
    )
    assert order1.status == OrderStatus.SCHEDULED and txn1 is None
    assert trading.committed_cash(db, taxable.id) == Decimal("8600.00")
    assert trading.buying_power(db, taxable.id) == Decimal("0.00")

    with pytest.raises(HTTPException) as exc:
        trading.place_order(db, taxable, _buy(taxable.id, "3333"), OrderSource.API, now=SATURDAY)
    assert exc.value.status_code == 422
    assert "already committed to open orders" in exc.value.detail
    assert Decimal(taxable.settlement_balance) == Decimal("8600")  # untouched


def test_cancelling_an_order_frees_its_cash(db, taxable, enforce_hours):
    taxable.settlement_balance = Decimal("5000")
    taxable.allow_external_funding = False
    db.commit()
    order, _ = trading.place_order(
        db, taxable, _buy(taxable.id, "5000"), OrderSource.API, now=SATURDAY
    )
    assert trading.buying_power(db, taxable.id) == Decimal("0.00")
    order.status = OrderStatus.CANCELLED
    db.commit()
    assert trading.buying_power(db, taxable.id) == Decimal("5000.00")


def test_share_orders_commit_estimated_cost(db, taxable, enforce_hours):
    taxable.allow_external_funding = False
    db.commit()
    price = trading.market_data.quote("VOO").price
    trading.place_order(
        db, taxable,
        _buy(taxable.id, quantity_type=QuantityType.SHARES, quantity=Decimal("5")),
        OrderSource.API, now=SATURDAY,
    )
    committed = trading.committed_cash(db, taxable.id)
    assert abs(committed - price * 5) < Decimal("1.00")


# ---------------------------------------------------------------- auto-funding

def test_taxable_order_pulls_external_funds(db, taxable):
    taxable.settlement_balance = Decimal("100")
    db.commit()
    order, txn = trading.place_order(db, taxable, _buy(taxable.id, "5000"), OrderSource.API)
    assert order.status == OrderStatus.FILLED
    assert txn is not None
    transfer = db.query(Contribution).filter_by(account_id=taxable.id).one()
    assert Decimal(transfer.amount) == Decimal("4900.00")   # only the shortfall
    assert transfer.kind == CashFlowKind.CONTRIBUTION
    assert "External bank transfer" in transfer.memo
    assert transfer.tax_year is None                        # taxable: no designation
    assert getattr(order, "funding_note", None) and "4900" in order.funding_note
    assert Decimal(taxable.settlement_balance) == Decimal("0.00")  # all cash deployed


def test_funding_disabled_blocks_the_order(db, taxable):
    taxable.settlement_balance = Decimal("100")
    taxable.allow_external_funding = False
    db.commit()
    with pytest.raises(HTTPException) as exc:
        trading.place_order(db, taxable, _buy(taxable.id, "5000"), OrderSource.API)
    assert "External funding is turned off" in exc.value.detail
    assert db.query(Contribution).count() == 0


def test_ira_funding_capped_by_contribution_limit(db, user, roth, limits, voo_asset):
    """An IRA can only pull in what its annual limit still allows."""
    roth.settlement_balance = Decimal("0")
    db.commit()
    # 2026 limit is 7500 (no catch-up for a 1990 birthdate)
    order, txn = trading.place_order(db, roth, _buy(roth.id, "5000"), OrderSource.API)
    assert order.status == OrderStatus.FILLED
    transfer = db.query(Contribution).filter_by(account_id=roth.id).one()
    assert Decimal(transfer.amount) == Decimal("5000.00")
    assert transfer.tax_year == date.today().year   # counts against the limit

    with pytest.raises(HTTPException) as exc:
        trading.place_order(db, roth, _buy(roth.id, "5000"), OrderSource.API)
    assert "IRA contribution limit" in exc.value.detail
    assert "2500" in exc.value.detail               # remaining room reported


def test_ira_at_limit_cannot_fund_at_all(db, user, roth, limits, voo_asset):
    db.add(Contribution(account_id=roth.id, tax_year=date.today().year,
                        amount=Decimal("7500"), kind=CashFlowKind.CONTRIBUTION))
    roth.settlement_balance = Decimal("0")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        trading.place_order(db, roth, _buy(roth.id, "100"), OrderSource.API)
    assert "contribution room left" in exc.value.detail


def test_external_funding_counts_toward_irs_status(db, user, roth, limits, voo_asset):
    from app.services import irs

    roth.settlement_balance = Decimal("0")
    db.commit()
    trading.place_order(db, roth, _buy(roth.id, "3000"), OrderSource.API)
    status = irs.irs_status(db, user, date.today().year)
    assert status.contributed == Decimal("3000.00")
    assert status.remaining == Decimal("4500.00")


def test_sells_are_never_auto_funded(db, taxable):
    """Auto-funding applies to purchases only."""
    trading.place_order(
        db, taxable, _buy(taxable.id, "1000", as_of=date.today() - timedelta(days=30)),
        OrderSource.API,
    )
    before = db.query(Contribution).count()
    trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("0.5")),
        OrderSource.API,
    )
    assert db.query(Contribution).count() == before
