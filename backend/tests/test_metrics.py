import math
from datetime import date, timedelta
from decimal import Decimal

from app.models import CashFlowKind, Contribution, OrderSource
from app.schemas import OrderCreateIn
from app.services import metrics, trading


def test_performance_with_backtest_order(db, user, taxable):
    db.add(Contribution(account_id=taxable.id, tax_year=None,
                        amount=Decimal("10000"), kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    order, txn = trading.place_order(
        db, taxable,
        OrderCreateIn(
            account_id=taxable.id, ticker="VOO", side="BUY",
            quantity_type="DOLLARS", quantity=Decimal("5000"),
            as_of=date.today() - timedelta(days=400),
        ),
        OrderSource.API,
    )
    assert txn is not None

    perf = metrics.performance(db, user, None, "all")
    assert len(perf.series) > 200
    # replayed value at the backtest date equals the invested amount (no phantom cash)
    first = perf.series[0]
    assert abs(float(first.value) - 5000.0) < 50
    # final point matches the live summary (cash + market value)
    summary = metrics.summary(db, user, None)
    assert abs(float(perf.series[-1].value) - float(summary.total_value)) < 1
    # metrics are finite and sane
    assert perf.twr_pct is not None and math.isfinite(perf.twr_pct)
    assert abs(perf.twr_pct) < 1000
    assert perf.irr_pct is None or math.isfinite(perf.irr_pct)
    # external money: +10000 contribution today, +5000 in-kind at as_of, -5000 cash at execution
    assert float(perf.series[0].net_deposits) == 5000.0
    assert float(perf.series[-1].net_deposits) == 10000.0


def test_performance_empty_portfolio(db, user, taxable):
    perf = metrics.performance(db, user, None, "1y")
    assert perf.series == []
    assert perf.twr_pct is None and perf.irr_pct is None
