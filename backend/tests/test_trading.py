from datetime import date, timedelta
from decimal import Decimal

from app.models import OrderSide, OrderSource, OrderStatus, OrderType, Position, QuantityType
from app.schemas import OrderCreateIn
from app.services import trading


def _order_in(account_id, **kw):
    base = dict(
        account_id=account_id, ticker="VOO", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity_type=QuantityType.DOLLARS,
        quantity=Decimal("500"),
    )
    base.update(kw)
    return OrderCreateIn(**base)


def test_market_buy_dollars(db, taxable):
    order, txn = trading.place_order(db, taxable, _order_in(taxable.id), OrderSource.API)
    assert order.status == OrderStatus.FILLED
    assert txn is not None
    assert txn.shares_filled == (Decimal("500") / txn.executed_price).quantize(
        Decimal("0.000001"), rounding="ROUND_DOWN"
    )
    assert Decimal(taxable.settlement_balance) == Decimal("9500.00")
    pos = db.query(Position).filter_by(account_id=taxable.id, ticker="VOO").one()
    assert Decimal(pos.shares) == txn.shares_filled


def test_insufficient_funds_rejected_when_cash_cannot_be_pulled_in(db, rollover):
    from fastapi import HTTPException
    import pytest

    rollover.settlement_balance = Decimal("10000")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, rollover, _order_in(rollover.id, quantity=Decimal("99999")), OrderSource.API
        )
    assert exc.value.status_code == 422
    assert "Insufficient buying power" in exc.value.detail
    assert Decimal(rollover.settlement_balance) == Decimal("10000")


def test_sell_realizes_pnl_and_clears_position(db, taxable):
    _, buy = trading.place_order(db, taxable, _order_in(taxable.id), OrderSource.API)
    order, sell = trading.place_order(
        db, taxable,
        _order_in(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
                  quantity=buy.shares_filled),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    assert sell.realized_gains is not None
    assert db.query(Position).filter_by(account_id=taxable.id, ticker="VOO").count() == 0


def test_sell_more_than_held_rejected(db, taxable):
    """Rejected up front, before an order is created."""
    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, taxable,
            _order_in(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
                      quantity=Decimal("5")),
            OrderSource.API,
        )
    assert exc.value.status_code == 422
    assert "Insufficient shares" in exc.value.detail


def test_historical_backtest_order(db, taxable):
    as_of = date.today() - timedelta(days=400)
    order, txn = trading.place_order(
        db, taxable, _order_in(taxable.id, as_of=as_of), OrderSource.API
    )
    assert order.status == OrderStatus.FILLED
    assert txn.as_of <= as_of
    assert txn.as_of >= as_of - timedelta(days=5)  # nearest prior business day


def test_average_cost_blends_across_buys(db, taxable):
    _, t1 = trading.place_order(db, taxable, _order_in(taxable.id), OrderSource.API)
    _, t2 = trading.place_order(
        db, taxable, _order_in(taxable.id, as_of=date.today() - timedelta(days=700)),
        OrderSource.API,
    )
    pos = db.query(Position).filter_by(account_id=taxable.id, ticker="VOO").one()
    total_shares = t1.shares_filled + t2.shares_filled
    assert Decimal(pos.shares) == total_shares
    blended = (t1.gross_amount + t2.gross_amount) / total_shares
    assert abs(Decimal(pos.average_cost) - blended) < Decimal("0.01")


def test_unknown_ticker_rejected(db, taxable):
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        trading.place_order(db, taxable, _order_in(taxable.id, ticker="ZZZZ"), OrderSource.API)
    assert exc.value.status_code == 422


def test_scheduling_next_run():
    from datetime import datetime, timezone

    from app.models import Cadence
    from app.services.scheduling import compute_next_run

    after = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)  # a Saturday
    monthly = compute_next_run(Cadence.MONTHLY, None, 1, after)
    assert (monthly.year, monthly.month, monthly.day) == (2026, 9, 1)
    weekly = compute_next_run(Cadence.WEEKLY, 0, None, after)
    assert weekly.weekday() == 0 and weekly > after
    daily = compute_next_run(Cadence.DAILY, None, None, after)
    assert daily.weekday() < 5 and daily > after


# ------------------------------------------------------------ variable slippage

def test_variable_slippage_is_bounded_deterministic_and_two_sided():
    """A static 2bp on every fill is the one thing real fills never look like.

    The draw has to stay inside the configured window, reproduce exactly for
    the same order (so replaying a backtest does not invent new fills), and
    occasionally come out favourable — retail flow does receive price
    improvement, and a model that only ever charges is not a model of the
    market."""
    from app.config import get_settings
    from app.services.trading import _slippage_bps

    s = get_settings()
    draws = [_slippage_bps(f"order-{i}") for i in range(3000)]
    assert all(s.slippage_bps_min <= float(d) <= s.slippage_bps_max for d in draws)
    assert _slippage_bps("order-7") == _slippage_bps("order-7")
    assert len(set(draws)) > 1000, "a distribution, not a constant"
    assert any(d < 0 for d in draws), "some fills should beat the quote"
    assert sum(d for d in draws) / len(draws) > 0, "but the mean stays adverse"


def test_slippage_moves_the_price_against_the_side(db, user, taxable, scenario):
    from decimal import Decimal as D

    from app.services.trading import _slipped

    px = D("100.0000")
    assert _slipped(px, OrderSide.BUY, "seed-a") > px
    assert _slipped(px, OrderSide.SELL, "seed-a") < px


def test_slippage_preview_is_stable(db):
    """A quote shown before the trade must not move on refresh, so the
    unseeded (preview) draw is the distribution's mode, not a sample."""
    from app.config import get_settings
    from app.services.trading import _slippage_bps

    assert _slippage_bps(None) == _slippage_bps(None) == get_settings().slippage_bps
