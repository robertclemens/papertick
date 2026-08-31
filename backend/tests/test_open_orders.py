"""Open-order accounting: share commitment, time-in-force, expiry, cadence."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Cadence,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    QuantityType,
    TimeInForce,
)
from app.schemas import OrderCreateIn
from app.services import market_calendar as cal
from app.services import trading
from app.services.scheduling import compute_next_run

WED_OPEN = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)     # 11:00 ET, open
WED_EVENING = datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc)  # after the close


def _order(account_id, **kw):
    base = dict(account_id=account_id, ticker="VOO", side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity_type=QuantityType.DOLLARS,
                quantity=Decimal("1000"))
    base.update(kw)
    return OrderCreateIn(**base)


def _seed_shares(db, taxable, dollars="5000"):
    _, txn = trading.place_order(
        db, taxable, _order(taxable.id, quantity=Decimal(dollars),
                            as_of=date.today() - timedelta(days=200)),
        OrderSource.API,
    )
    return Decimal(txn.shares_filled)


# ---------------------------------------------------------------- sell commitment

def test_pending_sell_commits_shares(db, taxable, enforce_hours):
    held = _seed_shares(db, taxable)
    price = trading.market_data.quote("VOO").price
    order, _ = trading.place_order(
        db, taxable,
        _order(taxable.id, side=OrderSide.SELL, order_type=OrderType.LIMIT,
               limit_price=price * 2, quantity_type=QuantityType.SHARES, quantity=held),
        OrderSource.API, now=WED_OPEN,
    )
    assert order.status == OrderStatus.PENDING
    assert trading.committed_shares(db, taxable.id, "VOO") == held
    assert trading.sellable_shares(db, taxable.id, "VOO") == Decimal("0")

    # the same shares cannot back a second sell
    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, taxable,
            _order(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
                   quantity=held),
            OrderSource.API, now=WED_OPEN,
        )
    assert exc.value.status_code == 422
    assert "already committed to open sell orders" in exc.value.detail


def test_cancelled_sell_releases_shares(db, taxable, enforce_hours):
    held = _seed_shares(db, taxable)
    price = trading.market_data.quote("VOO").price
    order, _ = trading.place_order(
        db, taxable,
        _order(taxable.id, side=OrderSide.SELL, order_type=OrderType.LIMIT,
               limit_price=price * 2, quantity_type=QuantityType.SHARES, quantity=held),
        OrderSource.API, now=WED_OPEN,
    )
    order.status = OrderStatus.CANCELLED
    db.commit()
    assert trading.sellable_shares(db, taxable.id, "VOO") == held


# ---------------------------------------------------------------- time in force

def test_day_order_expires_at_todays_close():
    exp = trading.expiry_for(TimeInForce.DAY, WED_OPEN)
    assert exp == cal.market_close_at(date(2026, 8, 26))
    # placed after the close: good through the next session
    exp2 = trading.expiry_for(TimeInForce.DAY, WED_EVENING)
    assert exp2 > WED_EVENING and exp2.date() == date(2026, 8, 27)


def test_gtc_windows_land_on_trading_days():
    for tif, days in ((TimeInForce.GTC_30, 30), (TimeInForce.GTC_90, 90),
                      (TimeInForce.GTC, 365)):
        exp = trading.expiry_for(tif, WED_OPEN)
        assert cal.is_trading_day(exp.date())
        assert abs((exp.date() - date(2026, 8, 26)).days - days) <= 5


def test_limit_order_defaults_to_gtc_60(db, taxable, enforce_hours):
    price = trading.market_data.quote("VOO").price
    order, _ = trading.place_order(
        db, taxable,
        _order(taxable.id, order_type=OrderType.LIMIT, limit_price=price / 2),
        OrderSource.API, now=WED_OPEN,
    )
    assert order.time_in_force == TimeInForce.GTC_60
    assert order.expires_at is not None
    assert 55 <= (order.expires_at.date() - date(2026, 8, 26)).days <= 65


def test_expired_order_releases_committed_cash(db, taxable, enforce_hours):
    taxable.settlement_balance = Decimal("5000")
    taxable.allow_external_funding = False
    db.commit()
    price = trading.market_data.quote("VOO").price
    order, _ = trading.place_order(
        db, taxable,
        _order(taxable.id, quantity=Decimal("5000"), order_type=OrderType.LIMIT,
               limit_price=price / 2, time_in_force=TimeInForce.DAY),
        OrderSource.API, now=WED_OPEN,
    )
    assert order.status == OrderStatus.PENDING
    assert trading.buying_power(db, taxable.id) == Decimal("0.00")

    expired = trading.expire_due_orders(db, now=order.expires_at + timedelta(minutes=1))
    assert expired == 1
    db.refresh(order)
    assert order.status == OrderStatus.EXPIRED
    assert "Time in force" in order.reject_reason
    assert trading.buying_power(db, taxable.id) == Decimal("5000.00")  # cash released


def test_expiry_sweep_ignores_live_orders(db, taxable, enforce_hours):
    price = trading.market_data.quote("VOO").price
    trading.place_order(
        db, taxable,
        _order(taxable.id, order_type=OrderType.LIMIT, limit_price=price / 2,
               time_in_force=TimeInForce.GTC_90),
        OrderSource.API, now=WED_OPEN,
    )
    assert trading.expire_due_orders(db, now=WED_EVENING) == 0


# ---------------------------------------------------------------- cadence

def test_quarterly_and_annual_cadence():
    after = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    q = compute_next_run(Cadence.QUARTERLY, None, 15, after, month_of_year=2)
    # anchored on February: Feb/May/Aug/Nov — next is Nov 15 (Aug 15 has passed)
    assert (q.year, q.month) == (2026, 11)
    assert q.day in (15, 16, 17)  # rolled to the next trading day if needed

    a = compute_next_run(Cadence.ANNUALLY, None, 2, after, month_of_year=1)
    assert (a.year, a.month) == (2027, 1)

    a2 = compute_next_run(Cadence.ANNUALLY, None, 1, after, month_of_year=12)
    assert (a2.year, a2.month) == (2026, 12)


def test_quarterly_advances_three_months(db, taxable):
    from app.models import RecurringRule
    from app.services.scheduling import advance_rule

    rule = RecurringRule(
        account_id=taxable.id, ticker="VOO", amount=Decimal("500"),
        cadence=Cadence.QUARTERLY, day_of_month=10, month_of_year=1,
        next_run_at=datetime(2026, 1, 12, 14, 30, tzinfo=timezone.utc),
    )
    nxt = advance_rule(rule, datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
    assert (nxt.year, nxt.month) == (2026, 7)


# ---------------------------------------------------------------- reporting

def test_summary_reports_committed_and_available(db, user, taxable, enforce_hours):
    from app.services import metrics

    taxable.settlement_balance = Decimal("10000")
    taxable.allow_external_funding = False
    db.commit()
    trading.place_order(db, taxable, _order(taxable.id, quantity=Decimal("4000")),
                        OrderSource.API, now=datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc))
    s = metrics.summary(db, user, None)
    assert s.cash == Decimal("10000.00")
    assert s.committed_cash == Decimal("4000.00")
    assert s.available_to_trade == Decimal("6000.00")
    assert s.open_order_count == 1


def test_withdrawal_blocked_by_committed_cash(db, user, taxable, enforce_hours):
    from app.routers.accounts import withdraw
    from app.deps import SESSION_SCOPES, Principal
    from app.schemas import WithdrawIn

    taxable.settlement_balance = Decimal("5000")
    taxable.allow_external_funding = False
    db.commit()
    trading.place_order(db, taxable, _order(taxable.id, quantity=Decimal("4000")),
                        OrderSource.API, now=datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc))
    principal = Principal(user=user, scopes=set(SESSION_SCOPES))
    with pytest.raises(HTTPException) as exc:
        withdraw(taxable.id, WithdrawIn(amount=Decimal("2000")), principal, db)
    assert "committed to open orders" in exc.value.detail
    # the uncommitted remainder is still withdrawable
    result = withdraw(taxable.id, WithdrawIn(amount=Decimal("1000")), principal, db)
    assert Decimal(result.account.settlement_balance) == Decimal("4000.00")
