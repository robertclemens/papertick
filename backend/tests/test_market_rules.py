from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Dividend,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    QuantityType,
)
from app.schemas import OrderCreateIn
from app.services import market_calendar as cal
from app.services import trading
from app.services.dividends import reconcile_account_ticker

WED_OPEN = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)   # Wed 11:00 ET, session open
WED_EVENING = datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc)  # Wed 18:00 ET, after close
SATURDAY = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)


def _buy(account_id, **kw):
    base = dict(account_id=account_id, ticker="VOO", side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity_type=QuantityType.DOLLARS,
                quantity=Decimal("500"))
    base.update(kw)
    return OrderCreateIn(**base)


# ---------------------------------------------------------------- calendar

def test_calendar_holidays_and_hours():
    assert not cal.is_trading_day(date(2026, 7, 3))    # July 4 observed (Saturday)
    assert not cal.is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not cal.is_trading_day(date(2026, 4, 3))    # Good Friday
    assert not cal.is_trading_day(date(2026, 12, 25))
    assert cal.is_trading_day(date(2026, 8, 26))
    # DST-correct opens: 13:30 UTC in August (EDT), 14:30 UTC in December (EST)
    assert cal.market_open_at(date(2026, 8, 26)).hour == 13
    assert cal.market_open_at(date(2026, 12, 15)).hour == 14
    assert cal.is_market_open(WED_OPEN)
    assert not cal.is_market_open(WED_EVENING)
    assert not cal.is_market_open(SATURDAY)
    nxt = cal.next_market_open(SATURDAY)
    assert nxt.date() == date(2026, 8, 31) and nxt.hour == 13


def test_nav_date_cutoff():
    assert cal.nav_date_for(WED_OPEN) == date(2026, 8, 26)       # before 4pm ET -> today's NAV
    assert cal.nav_date_for(WED_EVENING) == date(2026, 8, 27)    # after cutoff -> next day
    assert cal.nav_date_for(SATURDAY) == date(2026, 8, 31)


# ---------------------------------------------------------------- hours gating

def test_market_closed_queues_for_next_open(db, taxable, enforce_hours):
    order, txn = trading.place_order(db, taxable, _buy(taxable.id), OrderSource.API, now=SATURDAY)
    assert order.status == OrderStatus.SCHEDULED
    assert txn is None
    assert order.scheduled_for == cal.next_market_open(SATURDAY)


def test_market_open_fills_immediately(db, taxable, enforce_hours):
    order, txn = trading.place_order(db, taxable, _buy(taxable.id), OrderSource.API, now=WED_OPEN)
    assert order.status == OrderStatus.FILLED
    assert txn is not None


def test_mutual_fund_routes_to_nav(db, taxable, fund_asset, enforce_hours):
    order, txn = trading.place_order(
        db, taxable, _buy(taxable.id, ticker="VFIAX"), OrderSource.API, now=WED_OPEN
    )
    assert order.status == OrderStatus.SCHEDULED
    assert order.nav_date == date(2026, 8, 26)
    assert order.scheduled_for == cal.mf_fill_time(date(2026, 8, 26))
    # after the 4pm cutoff the next trading day's NAV applies
    order2, _ = trading.place_order(
        db, taxable, _buy(taxable.id, ticker="VFIAX"), OrderSource.API, now=WED_EVENING
    )
    assert order2.nav_date == date(2026, 8, 27)


def test_mutual_fund_rejects_limit_orders(db, taxable, fund_asset):
    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, taxable,
            _buy(taxable.id, ticker="VFIAX", order_type=OrderType.LIMIT, limit_price=Decimal("100")),
            OrderSource.API,
        )
    assert exc.value.status_code == 422


def test_nav_order_fills_at_that_days_close(db, taxable, fund_asset, enforce_hours):
    order, _ = trading.place_order(
        db, taxable, _buy(taxable.id, ticker="VFIAX"), OrderSource.API, now=WED_OPEN
    )
    processed = trading.run_due_scheduled_orders(db)  # real now is past the fill time
    assert processed == 1
    db.refresh(order)
    assert order.status == OrderStatus.FILLED
    from app.models import Transaction
    txn = db.query(Transaction).filter_by(order_id=order.id).one()
    assert txn.as_of == date(2026, 8, 26)
    from app.services.market_data import market_data
    assert Decimal(txn.executed_price) == market_data.close_exact("VFIAX", date(2026, 8, 26))


# ---------------------------------------------------------------- tax lots

def test_sell_splits_short_and_long_term(db, taxable):
    old = date.today() - timedelta(days=400)
    recent = date.today() - timedelta(days=30)
    _, t1 = trading.place_order(db, taxable, _buy(taxable.id, quantity=Decimal("3000"), as_of=old), OrderSource.API)
    _, t2 = trading.place_order(db, taxable, _buy(taxable.id, quantity=Decimal("3000"), as_of=recent), OrderSource.API)
    total_shares = t1.shares_filled + t2.shares_filled
    order, sell = trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES, quantity=total_shares),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    assert sell.realized_st is not None and sell.realized_lt is not None
    assert Decimal(sell.realized_st) + Decimal(sell.realized_lt) == Decimal(sell.realized_gains)
    price = Decimal(sell.executed_price)
    expect_lt = ((price - Decimal(t1.gross_amount) / t1.shares_filled) * t1.shares_filled)
    assert abs(Decimal(sell.realized_lt) - expect_lt) < Decimal("0.05")


def test_backdated_sell_cannot_use_later_lots(db, taxable):
    trading.place_order(db, taxable, _buy(taxable.id), OrderSource.API)  # acquired today
    order, txn = trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("1"), as_of=date.today() - timedelta(days=100)),
        OrderSource.API,
    )
    assert order.status == OrderStatus.REJECTED
    assert "as of" in order.reject_reason


# ---------------------------------------------------------------- dividends

def test_backtest_backfills_dividends(db, taxable):
    start_cash = Decimal(taxable.settlement_balance)
    _, txn = trading.place_order(
        db, taxable, _buy(taxable.id, quantity=Decimal("5000"), as_of=date.today() - timedelta(days=730)),
        OrderSource.API,
    )
    divs = db.query(Dividend).filter_by(account_id=taxable.id, ticker="VOO").all()
    assert len(divs) >= 6  # ~quarterly over two years
    total = sum(Decimal(d.amount) for d in divs)
    assert total > 0
    expected_cash = start_cash - Decimal(txn.gross_amount) - Decimal(txn.fees) + total
    assert Decimal(taxable.settlement_balance) == expected_cash
    # reconciliation is idempotent
    assert reconcile_account_ticker(db, taxable.id, "VOO") == 0


def test_tax_report_after_activity(db, user, taxable):
    from app.services.tax import tax_report

    _, t1 = trading.place_order(
        db, taxable, _buy(taxable.id, quantity=Decimal("5000"), as_of=date.today() - timedelta(days=730)),
        OrderSource.API,
    )
    trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES, quantity=t1.shares_filled),
        OrderSource.API,
    )
    report = tax_report(db, user, date.today().year, taxable.scenario_id)
    assert report.long_term_gains != 0
    assert report.short_term_gains == 0
    assert report.dividends > 0
    assert report.unclassified_gains == 0
