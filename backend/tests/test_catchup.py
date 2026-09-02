"""Recurring investments survive an outage.

The worker used to fire a single order at today's price no matter how many
runs it had slept through, so a three-day gap silently became one mispriced
buy. These lock in the replacement: every missed occurrence is rebuilt at its
own date and its own closing price.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import Cadence, Order, OrderStatus, RecurringRule, RuleStatus, Transaction
from app.services import market_calendar as cal
from app.services.market_data import market_data
from app.workers.tasks import _due_runs, run_recurring_investments


def _rule(db, account, days_ago: int, cadence=Cadence.DAILY) -> RecurringRule:
    """A rule whose next run fell `days_ago` trading days back."""
    d = date.today()
    for _ in range(days_ago):
        d = cal.previous_trading_day(d) if hasattr(cal, "previous_trading_day") else d - timedelta(days=1)
    rule = RecurringRule(
        account_id=account.id, ticker="VOO", amount=Decimal("100"),
        cadence=cadence, next_run_at=cal.market_open_at(cal.next_trading_day(d)),
        status=RuleStatus.ACTIVE,
    )
    db.add(rule)
    db.commit()
    return rule


def test_missed_runs_are_enumerated_not_collapsed(db, user, taxable, scenario):
    rule = _rule(db, taxable, days_ago=5)
    runs = _due_runs(rule, datetime.now(timezone.utc))
    assert len(runs) > 1, "an outage should owe more than one run"
    assert runs == sorted(runs), "oldest first"
    assert all(r <= datetime.now(timezone.utc) for r in runs)


def test_catchup_window_bounds_how_far_back_a_replay_reaches(db, user, taxable, scenario, monkeypatch):
    from app.config import get_settings

    rule = _rule(db, taxable, days_ago=90)
    settings = get_settings()
    monkeypatch.setattr(settings, "max_catchup_days", 7, raising=False)
    runs = _due_runs(rule, datetime.now(timezone.utc))
    floor = datetime.now(timezone.utc) - timedelta(days=7)
    assert runs and all(r >= floor for r in runs)


def test_catchup_disabled_falls_back_to_a_single_run(db, user, taxable, scenario, monkeypatch):
    from app.config import get_settings

    rule = _rule(db, taxable, days_ago=5)
    monkeypatch.setattr(get_settings(), "catchup_missed_runs", False, raising=False)
    assert _due_runs(rule, datetime.now(timezone.utc)) == [rule.next_run_at]


def test_backfilled_buys_are_priced_and_dated_to_the_day_they_were_due(
    db, user, taxable, scenario
):
    """The whole point: the ledger ends up where it would have been."""
    taxable.settlement_balance = Decimal("100000")
    rule = _rule(db, taxable, days_ago=6)
    expected = _due_runs(rule, datetime.now(timezone.utc))
    db.commit()

    run_recurring_investments()
    db.expire_all()

    orders = db.query(Order).filter(Order.account_id == taxable.id).all()
    filled = [o for o in orders if o.status == OrderStatus.FILLED]
    assert len(filled) == len(expected), "one order per missed run"

    # Each run is effective on its own due date, not on today. The check reads
    # the transaction rather than the order: `Order.as_of` is the backtest
    # marker and is legitimately None for a run that is due today, while
    # `Transaction.as_of` always carries the effective date.
    txns = db.query(Transaction).filter(Transaction.account_id == taxable.id).all()
    assert {t.as_of for t in txns} == {r.date() for r in expected}
    assert all(o.as_of == o.created_at.date() or o.as_of is None
               for o in filled if o.as_of is None or o.as_of >= date.today())

    # and priced at that day's close rather than today's
    for txn in db.query(Transaction).filter(Transaction.account_id == taxable.id):
        if txn.as_of < date.today():
            close = market_data.close_on("VOO", txn.as_of)
            assert close is not None
            assert Decimal(txn.executed_price) == close
            assert txn.executed_at.date() == txn.as_of

    # the rule is left pointing forward, so the next tick does not replay
    db.refresh(rule)
    # sqlite has no tz type; stored times are UTC either way
    nxt = rule.next_run_at
    nxt = nxt if nxt.tzinfo else nxt.replace(tzinfo=timezone.utc)
    assert nxt > datetime.now(timezone.utc)


def test_a_second_tick_does_not_duplicate_the_catch_up(db, user, taxable, scenario):
    taxable.settlement_balance = Decimal("100000")
    _rule(db, taxable, days_ago=4)
    db.commit()

    run_recurring_investments()
    db.expire_all()
    first = db.query(Order).filter(Order.account_id == taxable.id).count()
    assert first > 1

    run_recurring_investments()
    db.expire_all()
    assert db.query(Order).filter(Order.account_id == taxable.id).count() == first
