from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Contribution,
    CashFlowKind,
    CostBasisMethod,
    IrsLimit,
    OptionAction,
    OptionPosition,
    OptionRight,
    OptionTransaction,
    OrderSide,
    OrderSource,
    OrderStatus,
    PositionSide,
    QuantityType,
    TaxLot,
    utcnow,
)
from app.schemas import OptionOrderIn, OrderCreateIn, SpecLotIn
from app.services import options as opt
from app.services import trading


def _buy(account_id, **kw):
    base = dict(account_id=account_id, ticker="VOO", side=OrderSide.BUY,
                order_type="MARKET", quantity_type=QuantityType.DOLLARS,
                quantity=Decimal("1000"))
    base.update(kw)
    return OrderCreateIn(**base)


def _three_lots(db, taxable):
    """Backtest buys at three dates -> three lots with different costs."""
    dates = [date.today() - timedelta(days=n) for n in (500, 300, 30)]
    txns = []
    for d in dates:
        _, t = trading.place_order(db, taxable, _buy(taxable.id, as_of=d), OrderSource.API)
        assert t is not None
        txns.append(t)
    return dates, txns


def _lots(db, account_id):
    return db.query(TaxLot).filter_by(account_id=account_id, ticker="VOO").order_by(TaxLot.acquired_on).all()


def test_lifo_consumes_newest_lot(db, taxable):
    dates, _ = _three_lots(db, taxable)
    order, sell = trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("1"), cost_basis_method=CostBasisMethod.LIFO),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    remaining = _lots(db, taxable.id)
    newest = max(remaining, key=lambda l: l.acquired_on)
    assert newest.acquired_on == dates[2]  # newest lot partially consumed, still present
    # oldest two untouched
    assert all(Decimal(l.shares_open) > 0 for l in remaining)


def test_hifo_consumes_highest_cost_lot(db, taxable):
    _three_lots(db, taxable)
    lots_before = _lots(db, taxable.id)
    highest = max(lots_before, key=lambda l: Decimal(l.cost_per_share))
    before_shares = Decimal(highest.shares_open)
    trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("1"), cost_basis_method=CostBasisMethod.HIFO),
        OrderSource.API,
    )
    db.refresh(highest)
    assert Decimal(highest.shares_open) == before_shares - 1


def test_spec_id_sells_exact_lot(db, taxable):
    _three_lots(db, taxable)
    target = _lots(db, taxable.id)[1]  # the middle lot
    before = Decimal(target.shares_open)
    order, sell = trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("1"),
             spec_lots=[SpecLotIn(lot_id=target.id, shares=Decimal("1"))]),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    assert order.cost_basis_method == CostBasisMethod.SPEC_ID
    db.refresh(target)
    assert Decimal(target.shares_open) == before - 1


def test_spec_id_total_mismatch_rejected(db, taxable):
    _three_lots(db, taxable)
    target = _lots(db, taxable.id)[0]
    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, taxable,
            _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
                 quantity=Decimal("5"),
                 spec_lots=[SpecLotIn(lot_id=target.id, shares=Decimal("2"))]),
            OrderSource.API,
        )
    assert exc.value.status_code == 422


def test_average_cost_only_for_funds(db, taxable):
    _three_lots(db, taxable)
    order, _ = trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("1"), cost_basis_method=CostBasisMethod.AVERAGE),
        OrderSource.API,
    )
    assert order.status == OrderStatus.REJECTED
    assert "mutual funds" in order.reject_reason


def test_average_cost_on_fund_rebases_lots(db, taxable, fund_asset):
    for days in (400, 40):
        trading.place_order(
            db, taxable,
            _buy(taxable.id, ticker="VFIAX", as_of=date.today() - timedelta(days=days)),
            OrderSource.API,
        )
    lots = db.query(TaxLot).filter_by(account_id=taxable.id, ticker="VFIAX").all()
    avg = sum(Decimal(l.shares_open) * Decimal(l.cost_per_share) for l in lots) / \
        sum(Decimal(l.shares_open) for l in lots)
    order, sell = trading.place_order(
        db, taxable,
        _buy(taxable.id, ticker="VFIAX", side=OrderSide.SELL,
             quantity_type=QuantityType.SHARES, quantity=Decimal("3"),
             cost_basis_method=CostBasisMethod.AVERAGE),
        OrderSource.API,
    )
    assert order.status == OrderStatus.FILLED
    expected = (Decimal(sell.executed_price) - avg) * 3
    assert abs(Decimal(sell.realized_gains) - expected) < Decimal("0.05")
    for l in db.query(TaxLot).filter_by(account_id=taxable.id, ticker="VFIAX").all():
        assert abs(Decimal(l.cost_per_share) - avg) < Decimal("0.01")


def test_min_tax_prefers_losses(db, taxable):
    _three_lots(db, taxable)
    price = trading.market_data.quote("VOO").price
    lots = _lots(db, taxable.id)
    losers = [l for l in lots if Decimal(l.cost_per_share) > price]
    if not losers:
        pytest.skip("synthetic path produced no losing lot")
    order, sell = trading.place_order(
        db, taxable,
        _buy(taxable.id, side=OrderSide.SELL, quantity_type=QuantityType.SHARES,
             quantity=Decimal("0.5"), cost_basis_method=CostBasisMethod.MIN_TAX),
        OrderSource.API,
    )
    assert Decimal(sell.realized_gains) < 0  # a losing lot was sold first


# ---------------------------------------------------------------- IRS auto-update

def test_ensure_limits_projects_next_year(db, limits):
    from app.services.irs import ensure_limits, tax_day

    created = ensure_limits(db, today=date(2026, 8, 29))
    assert 2027 in created
    row = db.get(IrsLimit, 2027)
    assert row.source == "projected"
    assert Decimal(row.ira_limit) == Decimal("7500")  # carried from 2026
    assert row.designation_deadline == tax_day(2028)
    # idempotent
    assert ensure_limits(db, today=date(2026, 8, 29)) == []


# ---------------------------------------------------------------- options

WED_OPEN = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)


def _option_order(account_id, **kw):
    base = dict(account_id=account_id, underlying="VOO", right=OptionRight.CALL,
                strike=Decimal("100"), expiry=opt.list_expirations()[2],
                action="BUY_TO_OPEN", contracts=1)
    base.update(kw)
    return OptionOrderIn(**base)


def test_expirations_are_trading_days():
    from app.services import market_calendar as cal

    exps = opt.list_expirations()
    assert len(exps) >= 8
    assert all(cal.is_trading_day(d) and d > date.today() for d in exps)


def test_put_call_parity():
    spot = Decimal("100")
    expiry = opt.list_expirations()[3]
    call = opt.option_quote("VOO", OptionRight.CALL, Decimal("100"), expiry, spot)
    put = opt.option_quote("VOO", OptionRight.PUT, Decimal("100"), expiry, spot)
    import math
    from app.config import get_settings
    t = opt._t_years(expiry)
    parity = float(spot) - 100 * math.exp(-get_settings().risk_free_rate * t)
    assert abs(float(call.mid - put.mid) - parity) < 0.6


def test_long_call_lifecycle(db, taxable):
    spot = trading.market_data.quote("VOO").price
    strike = opt.strike_grid(spot)[len(opt.strike_grid(spot)) // 2]
    cash0 = Decimal(taxable.settlement_balance)
    pos, txn, explanation = opt.open_position(
        db, taxable, _option_order(taxable.id, strike=strike), now=WED_OPEN
    )
    assert pos.side == PositionSide.LONG and pos.contracts == 1
    assert "right" in explanation.lower() and "100 shares" in explanation
    db.refresh(taxable)
    assert Decimal(taxable.settlement_balance) < cash0
    close_txn, _ = opt.close_position(db, pos, 1, now=WED_OPEN)
    assert close_txn.realized_gains is not None
    assert db.get(OptionPosition, pos.id) is None


def test_covered_call_requires_shares(db, taxable):
    spot = trading.market_data.quote("VOO").price
    strike = opt.strike_grid(spot)[-1]
    with pytest.raises(HTTPException) as exc:
        opt.open_position(
            db, taxable,
            _option_order(taxable.id, strike=strike, action="SELL_TO_OPEN"),
            now=WED_OPEN,
        )
    assert "Covered calls" in exc.value.detail
    # buy 100+ shares, then it works and credits premium
    taxable.settlement_balance = Decimal("200000")
    db.commit()
    stock_order, stock_txn = trading.place_order(
        db, taxable,
        _buy(taxable.id, quantity_type=QuantityType.SHARES, quantity=Decimal("100"),
             as_of=date.today() - timedelta(days=200)),
        OrderSource.API,
    )
    assert stock_txn is not None, stock_order.reject_reason
    cash0 = Decimal(db.get(type(taxable), taxable.id).settlement_balance)
    pos, txn, explanation = opt.open_position(
        db, taxable, _option_order(taxable.id, strike=strike, action="SELL_TO_OPEN"), now=WED_OPEN
    )
    assert pos.side == PositionSide.SHORT
    assert "OBLIGATED" in explanation
    db.refresh(taxable)
    assert Decimal(taxable.settlement_balance) > cash0


def test_cash_secured_put_reserves_collateral(db, taxable):
    taxable.settlement_balance = Decimal("200000")
    db.commit()
    spot = trading.market_data.quote("VOO").price
    strike = opt.strike_grid(spot)[0]
    pos, txn, explanation = opt.open_position(
        db, taxable,
        _option_order(taxable.id, right=OptionRight.PUT, strike=strike, action="SELL_TO_OPEN"),
        now=WED_OPEN,
    )
    assert Decimal(pos.collateral) == strike * 100
    assert trading.reserved_cash(db, taxable.id) == strike * 100
    assert "CASH-SECURED" in explanation


def test_otm_long_option_expires_worthless(db, taxable):
    spot = trading.market_data.quote("VOO").price
    far_strike = (spot * Decimal("2")).quantize(Decimal("1"))
    pos = OptionPosition(
        account_id=taxable.id, underlying="VOO", right=OptionRight.CALL,
        strike=far_strike, expiry=date.today() - timedelta(days=3),
        side=PositionSide.LONG, contracts=2, avg_premium=Decimal("1.50"),
        collateral=0, opened_on=date.today() - timedelta(days=60),
    )
    db.add(pos)
    db.commit()
    processed = opt.process_expirations(db)
    assert processed == 1
    assert db.get(OptionPosition, pos.id) is None
    txn = db.query(OptionTransaction).filter_by(
        account_id=taxable.id, action=OptionAction.EXPIRATION).one()
    assert Decimal(txn.realized_gains) == Decimal("-300.00")  # 1.50 x 100 x 2
    assert Decimal(txn.realized_st) == Decimal("-300.00")


# ---------------------------------------------------------------- statements

def test_statement_generation(db, user, taxable):
    from app.models import Statement
    from app.services.statements import generate_missing

    past = date.today() - timedelta(days=100)
    db.add(Contribution(account_id=taxable.id, tax_year=None, amount=Decimal("5000"),
                        kind=CashFlowKind.CONTRIBUTION,
                        timestamp=datetime(past.year, past.month, past.day, tzinfo=timezone.utc)))
    db.commit()
    # the backdated fill restates the periods it touches on its own
    trading.place_order(db, taxable, _buy(taxable.id, as_of=past), OrderSource.API)
    stmts = db.query(Statement).filter_by(user_id=user.id).all()
    assert len(stmts) >= 2  # several completed months since then
    assert all(s.pdf.startswith(b"%PDF") for s in stmts)
    assert all(len(s.pdf) > 2000 for s in stmts)
    # idempotent
    assert generate_missing(db, user, scenario_id=taxable.scenario_id) == 0


def test_backdated_trade_restates_affected_statements(db, user, taxable):
    """A past-dated fill changes balances inside periods that were already
    issued, so those statements are re-rendered and earlier ones are not."""
    from app.models import Statement
    from app.services.statements import generate_missing

    old_day = date.today() - timedelta(days=200)
    db.add(Contribution(account_id=taxable.id, tax_year=None, amount=Decimal("20000"),
                        kind=CashFlowKind.CONTRIBUTION,
                        timestamp=datetime(old_day.year, old_day.month, old_day.day,
                                           tzinfo=timezone.utc)))
    db.commit()
    generate_missing(db, user, scenario_id=taxable.scenario_id)

    before = {
        (s.kind, s.period_start): (s.id, s.pdf)
        for s in db.query(Statement).filter_by(user_id=user.id).all()
    }
    assert len(before) >= 4

    cutoff = date.today() - timedelta(days=60)
    trading.place_order(db, taxable, _buy(taxable.id, as_of=cutoff), OrderSource.API)

    after = {
        (s.kind, s.period_start): (s.id, s.pdf)
        for s in db.query(Statement).filter_by(user_id=user.id).all()
    }
    assert set(after) == set(before)          # same periods, nothing lost
    touched = [k for k in before if k[1] >= cutoff.replace(day=1)]
    untouched = [k for k in before if k[1] < cutoff.replace(day=1)]
    assert touched and untouched
    # periods covering the backdated date were re-rendered as new rows
    assert all(after[k][0] != before[k][0] for k in touched)
    # everything before it is byte-for-byte the archived original
    assert all(after[k] == before[k] for k in untouched)


def test_backdated_trades_are_off_by_default(db, taxable, monkeypatch):
    from fastapi import HTTPException

    from app.config import get_settings

    locked = get_settings().model_copy(update={"allow_backdated_trades": False})
    monkeypatch.setattr("app.services.trading.get_settings", lambda: locked)

    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, taxable, _buy(taxable.id, as_of=date.today() - timedelta(days=30)),
            OrderSource.API,
        )
    assert exc.value.status_code == 422
    assert "ALLOW_BACKDATED_TRADES" in exc.value.detail

    # same ticket without a past date still goes through
    order, txn = trading.place_order(db, taxable, _buy(taxable.id), OrderSource.API)
    assert txn is not None

def test_activity_feed_sorts_by_effective_date(db, user, taxable):
    """A past-dated fill is entered today but belongs at its own date in an
    activity feed; the audit view still leads with what ran most recently."""
    from app.deps import Principal
    from app.models import Scenario
    from app.routers.orders import list_transactions

    trading.place_order(db, taxable, _buy(taxable.id), OrderSource.API)          # today
    trading.place_order(db, taxable,
                        _buy(taxable.id, as_of=date.today() - timedelta(days=200)),
                        OrderSource.API)                                          # backdated
    db.commit()

    principal = Principal(user=user, scopes={"read"}, via_api_key=False,
                          scenario=db.get(Scenario, taxable.scenario_id))
    feed = list_transactions(None, 10, "effective", principal, db)
    assert [t.as_of for t in feed] == sorted((t.as_of for t in feed), reverse=True)
    # the "today" order's as_of is stamped by the app as utcnow().date() (ledger
    # dates are UTC everywhere), which can differ from local date.today() near
    # midnight in timezones behind UTC -- compare like for like.
    assert feed[0].as_of == utcnow().date()

    audit = list_transactions(None, 10, "executed", principal, db)
    assert audit[0].as_of == date.today() - timedelta(days=200)  # entered last


def test_reconciler_leaves_imported_dividends_alone(db, taxable, monkeypatch):
    """An imported holding's payment history is the brokerage's record. The
    ex-date reconciler must not restate it, nor invent payments for dates the
    export does not contain."""
    from datetime import date as _date

    from app.models import Dividend
    from app.services import dividends as div

    trading.place_order(db, taxable, _buy(taxable.id), OrderSource.API)
    db.add(Dividend(account_id=taxable.id, ticker="VOO",
                    event_date=_date.today() - timedelta(days=5),
                    per_share=Decimal("0"), shares=Decimal("0"),
                    amount=Decimal("12.34"), imported=True))
    db.commit()
    before = Decimal(taxable.settlement_balance)

    monkeypatch.setattr(div.market_data, "dividends", lambda t, s, e: [
        (_date.today() - timedelta(days=5), Decimal("9.99")),   # would restate
        (_date.today() - timedelta(days=2), Decimal("1.11")),   # would invent
    ])
    assert div.reconcile_account_ticker(db, taxable.id, "VOO") == Decimal("0")
    db.commit()

    rows = db.query(Dividend).filter_by(account_id=taxable.id, ticker="VOO").all()
    assert len(rows) == 1 and Decimal(rows[0].amount) == Decimal("12.34")
    assert Decimal(taxable.settlement_balance) == before


# ---------------------------------------------------------------- max funding

def _plan(db, account, cadence, **kw):
    from app.services import irs

    return irs.max_funding_plan(db, account, cadence, kw.get("dow"), kw.get("dom"),
                                kw.get("moy"), now=kw.get("now"))


def test_max_funding_splits_room_and_totals_exactly(db, user, roth, limits):
    """Per-run amounts round up so the trimmed final run lands the year exactly
    on the limit — never a cent or two under it."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.models import Cadence, CashFlowKind, Contribution

    year = date.today().year
    db.add(Contribution(account_id=roth.id, tax_year=year, amount=Decimal("5048.09"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()

    now = _dt(year, 8, 30, 12, 0, tzinfo=_tz.utc)
    plan = _plan(db, roth, Cadence.WEEKLY, dow=0, now=now)

    assert plan.eligible and plan.tax_year == year
    assert plan.remaining == Decimal("2451.91")
    assert plan.runs > 1
    assert plan.last_run.date().year == year and plan.last_run.date().month == 12
    # every run but the last is identical; the last absorbs the remainder
    assert plan.per_run * (plan.runs - 1) + plan.final_run == plan.remaining
    assert plan.final_run <= plan.per_run
    assert plan.total == plan.remaining


def test_max_funding_handles_a_single_remaining_run(db, user, roth, limits):
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.models import Cadence

    now = _dt(date.today().year, 12, 20, 12, 0, tzinfo=_tz.utc)
    plan = _plan(db, roth, Cadence.MONTHLY, dom=28, now=now)
    assert plan.runs == 1
    assert plan.per_run == plan.final_run == plan.remaining


def test_max_funding_reports_when_there_is_nothing_to_plan(db, user, roth, taxable, limits):
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.models import Cadence, CashFlowKind, Contribution

    year = date.today().year
    db.add(Contribution(account_id=roth.id, tax_year=year, amount=Decimal("99999"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    maxed = _plan(db, roth, Cadence.WEEKLY, dow=0,
                  now=_dt(year, 6, 1, 12, 0, tzinfo=_tz.utc))
    assert not maxed.eligible and maxed.remaining == Decimal("0")
    assert any("already fully contributed" in n for n in maxed.notes)

    # a taxable brokerage has no limit to fill
    none_needed = _plan(db, taxable, Cadence.WEEKLY, dow=0)
    assert not none_needed.eligible
    assert any("no annual contribution limit" in n for n in none_needed.notes)


def test_fund_to_limit_run_is_capped_at_remaining_room(db, user, roth, limits):
    """The rule asks for more than the room left; the run is trimmed to it."""
    from app.models import Cadence, CashFlowKind, Contribution, RecurringRule, RuleStatus
    from app.workers.tasks import _contribution_room

    year = date.today().year
    db.add(Contribution(account_id=roth.id, tax_year=year, amount=Decimal("7400"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    room = _contribution_room(db, roth.id)
    assert room == Decimal("100.00")

    rule = RecurringRule(account_id=roth.id, ticker="VOO", amount=Decimal("500"),
                         cadence=Cadence.WEEKLY, day_of_week=0, fund_to_limit=True,
                         next_run_at=datetime.now(timezone.utc), status=RuleStatus.ACTIVE)
    db.add(rule)
    db.commit()
    assert min(Decimal(rule.amount), room) == Decimal("100.00")

    # once the year is full the run has nothing to contribute
    db.add(Contribution(account_id=roth.id, tax_year=year, amount=Decimal("100"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    assert _contribution_room(db, roth.id) == Decimal("0")
