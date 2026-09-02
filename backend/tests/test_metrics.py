import math
from datetime import date, timedelta
from decimal import Decimal

from app.models import CashFlowKind, Contribution, OrderSource, utcnow
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


def _seed_history(db, taxable):
    """A deposit, a backtested buy 14 months back, and a later top-up.

    The settlement balance is credited alongside each contribution, the way the
    deposit endpoint does. Adding the ledger row alone leaves the account's real
    balance disagreeing with the replayed one, and the replay closes that gap
    with a residual transfer on the final day — a flow with no event behind it.
    """
    def deposit(amount: str, days_ago: int) -> None:
        db.add(Contribution(account_id=taxable.id, tax_year=None,
                            amount=Decimal(amount), kind=CashFlowKind.CONTRIBUTION,
                            timestamp=utcnow() - timedelta(days=days_ago)))
        taxable.settlement_balance = Decimal(taxable.settlement_balance) + Decimal(amount)

    deposit("20000", 430)
    db.commit()
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side="BUY",
                      quantity_type="DOLLARS", quantity=Decimal("8000"),
                      as_of=date.today() - timedelta(days=425)),
        OrderSource.API,
    )
    deposit("1500", 100)
    db.commit()


def test_every_month_balances_and_cumulative_runs_from_inception(db, user, taxable):
    """The table's whole contract, in one test.

    Each row must satisfy ending = beginning + flows + market + income, each
    month must open exactly where the previous one closed, and the cumulative
    column must be the running sum of personal returns — from inception, not
    from the top of whatever window was asked for.
    """
    _seed_history(db, taxable)

    full = metrics.monthly_performance(db, user, None, None)
    assert len(full.months) > 12

    for row in full.months:
        assert (row.beginning_balance + row.net_cash_flow + row.market_gain
                + row.income) == row.ending_balance, f"{row.month} does not balance"
        assert row.personal_return == row.market_gain + row.income

    # newest first, and each month opens where the previous one closed
    oldest_first = list(reversed(full.months))
    for prev, nxt in zip(oldest_first, oldest_first[1:]):
        assert nxt.beginning_balance == prev.ending_balance
        assert nxt.cumulative_return == prev.cumulative_return + nxt.personal_return

    # a shorter window shows fewer rows but does not restate the cumulative
    short = metrics.monthly_performance(db, user, None, 3)
    assert len(short.months) == 3
    assert short.months[0].cumulative_return == full.months[0].cumulative_return
    assert short.months[0].month == full.months[0].month


def test_the_monthly_table_ties_to_the_chart_above_it(db, user, taxable):
    """Both views come off the same replay, so the newest month's closing
    balance is the last point on the all-time chart."""
    _seed_history(db, taxable)

    chart = metrics.performance(db, user, None, "all")
    table = metrics.monthly_performance(db, user, None, None)

    assert table.months[0].ending_balance == chart.series[-1].value
    # and total personal return since inception matches the chart's own figure
    assert abs(table.months[0].cumulative_return
               - chart.investment_returns) < Decimal("0.05")


def test_month_events_are_the_month_that_was_asked_for(db, user, taxable):
    _seed_history(db, taxable)

    month = (utcnow() - timedelta(days=100)).strftime("%Y-%m")  # the second deposit
    events = metrics.month_events(db, user, month, None)

    assert events, "a month with cash flow must have something behind it"
    assert all(e.date.strftime("%Y-%m") == month for e in events)
    assert any(e.kind in ("CONTRIBUTION", "BUY", "SELL", "DIVIDEND") for e in events)
