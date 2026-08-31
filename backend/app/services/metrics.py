"""Portfolio valuation and performance metrics (gains, TWR, IRR).

The daily value series is rebuilt by replaying the ledger: contributions and
withdrawals move cash on their calendar date, fills move cash/shares on their
effective market date (`Transaction.as_of` — for backtested orders that is the
historical date, i.e. "pretend you invested then"). Prices come from the
market-data service with forward-fill over non-trading days.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    Asset,
    Contribution,
    Dividend,
    OrderSide,
    Position,
    Transaction,
    User,
    utcnow,
)
from app.schemas import (
    PerformanceOut,
    PerformancePointOut,
    PortfolioSummaryOut,
    PositionOut,
    AccountOut,
)
from app.services.market_data import EPOCH, MarketDataError, market_data

ZERO = Decimal("0")
CENT = Decimal("0.01")

RANGE_DAYS = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "3y": 1096, "5y": 1827,
              "10y": 3653, "all": None}


def _user_accounts(db: Session, user: User, account_id: str | None,
                   scenario_id: str | None = None) -> list[Account]:
    # the user's drag-and-drop ordering, so every list agrees with /accounts
    q = (
        select(Account).where(Account.user_id == user.id)
        .order_by(Account.sort_order, Account.created_at)
    )
    if scenario_id:
        q = q.where(Account.scenario_id == scenario_id)
    if account_id:
        q = q.where(Account.id == account_id)
    return list(db.execute(q).scalars())


def positions_view(db: Session, user: User, account_id: str | None = None,
                   scenario_id: str | None = None) -> list[PositionOut]:
    accounts = _user_accounts(db, user, account_id, scenario_id)
    ids = [a.id for a in accounts]
    if not ids:
        return []
    rows = db.execute(
        select(Position, Asset)
        .join(Asset, Asset.ticker == Position.ticker)
        .where(Position.account_id.in_(ids))
        .order_by(Position.ticker)
    ).all()
    out: list[PositionOut] = []
    for pos, asset in rows:
        shares = Decimal(pos.shares)
        avg = Decimal(pos.average_cost)
        try:
            price = market_data.quote(pos.ticker).price
        except MarketDataError:
            price = avg
        cost_basis = (shares * avg).quantize(CENT)
        market_value = (shares * price).quantize(CENT)
        unreal = market_value - cost_basis
        out.append(PositionOut(
            account_id=pos.account_id,
            ticker=pos.ticker,
            name=asset.name,
            asset_class=asset.asset_class,
            category=asset.category,
            region=asset.region,
            expense_ratio=asset.expense_ratio,
            prospectus_url=asset.prospectus_url,
            shares=shares,
            average_cost=avg,
            cost_basis=cost_basis,
            price=price,
            market_value=market_value,
            unrealized_gains=unreal,
            unrealized_gains_pct=float(unreal / cost_basis * 100) if cost_basis else None,
        ))
    return out


def summary(db: Session, user: User, account_id: str | None = None,
            scenario_id: str | None = None) -> PortfolioSummaryOut:
    from app.models import OPEN_STATUSES, Order, OrderSide
    from app.services.options import positions_value as options_positions_value
    from app.services.trading import account_out as _account_out
    from app.services.trading import committed_cash as get_committed
    from app.services.trading import reserved_cash as get_reserved

    accounts = _user_accounts(db, user, account_id, scenario_id)
    ids = [a.id for a in accounts]
    positions = positions_view(db, user, account_id, scenario_id)
    cash = sum((Decimal(a.settlement_balance) for a in accounts), ZERO)
    reserved = sum((get_reserved(db, aid) for aid in ids), ZERO)
    committed = sum((get_committed(db, aid) for aid in ids), ZERO)
    open_orders = 0
    if ids:
        open_orders = len(db.execute(
            select(Order.id).where(
                Order.account_id.in_(ids), Order.status.in_(OPEN_STATUSES)
            )
        ).all())
    options_value = options_positions_value(db, ids)
    invested = sum((p.market_value for p in positions), ZERO)
    basis = sum((p.cost_basis for p in positions), ZERO)
    unreal = sum((p.unrealized_gains for p in positions), ZERO)

    realized = ZERO
    realized_taxable = ZERO
    realized_sheltered = ZERO
    fees = ZERO
    deposits = ZERO
    dividends_total = ZERO
    taxable_ids = [a.id for a in accounts if a.account_type == AccountType.TAXABLE]
    sheltered_ids = [a.id for a in accounts if a.account_type != AccountType.TAXABLE]
    if ids:
        dividends_total = Decimal(db.execute(
            select(func.coalesce(func.sum(Dividend.amount), 0))
            .where(Dividend.account_id.in_(ids))
        ).scalar_one())
        def _realized(account_ids: list[str]) -> Decimal:
            if not account_ids:
                return ZERO
            return Decimal(db.execute(
                select(func.coalesce(func.sum(Transaction.realized_gains), 0))
                .where(Transaction.account_id.in_(account_ids))
            ).scalar_one())

        realized_taxable = _realized(taxable_ids)
        realized_sheltered = _realized(sheltered_ids)
        realized = realized_taxable + realized_sheltered
        fees = Decimal(db.execute(
            select(func.coalesce(func.sum(Transaction.fees), 0))
            .where(Transaction.account_id.in_(ids))
        ).scalar_one())
        deposits = Decimal(db.execute(
            select(func.coalesce(func.sum(Contribution.amount), 0))
            .where(Contribution.account_id.in_(ids))
        ).scalar_one())

    return PortfolioSummaryOut(
        total_value=(cash + invested + options_value).quantize(CENT),
        cash=cash.quantize(CENT),
        reserved_cash=reserved.quantize(CENT),
        committed_cash=committed.quantize(CENT),
        available_to_trade=(cash - reserved - committed).quantize(CENT),
        open_order_count=open_orders,
        invested_value=invested.quantize(CENT),
        options_value=options_value.quantize(CENT),
        cost_basis=basis.quantize(CENT),
        net_deposits=deposits.quantize(CENT),
        unrealized_gains=unreal.quantize(CENT),
        realized_gains=realized.quantize(CENT),
        realized_gains_taxable=realized_taxable.quantize(CENT),
        realized_gains_sheltered=realized_sheltered.quantize(CENT),
        total_dividends=dividends_total.quantize(CENT),
        total_fees=fees.quantize(CENT),
        accounts=[_account_out(db, a) for a in accounts],
    )


def account_returns(db: Session, user: User, range_key: str,
                    scenario_id: str | None = None):
    """Per-account balance and period performance — what an account list shows:
    current balance, investment returns and rate of return for the selected
    timeframe."""
    from app.schemas import AccountReturnOut, AccountReturnsOut
    from app.services.options import positions_value as options_positions_value

    accounts = _user_accounts(db, user, None, scenario_id)
    positions = positions_view(db, user, None, scenario_id)
    by_account: dict[str, Decimal] = defaultdict(Decimal)
    for pos in positions:
        by_account[pos.account_id] += pos.market_value

    rows: list[AccountReturnOut] = []
    for account in accounts:
        perf = performance(db, user, account.id, range_key, scenario_id)
        balance = (
            Decimal(account.settlement_balance)
            + by_account.get(account.id, ZERO)
            + options_positions_value(db, [account.id])
        )
        rows.append(AccountReturnOut(
            account_id=account.id,
            name=account.name,
            account_type=account.account_type,
            balance=balance.quantize(CENT),
            settlement_balance=Decimal(account.settlement_balance).quantize(CENT),
            investment_returns=perf.investment_returns,
            rate_of_return_pct=perf.rate_of_return_pct,
            rate_of_return_annualized=perf.rate_of_return_annualized,
        ))

    total = performance(db, user, None, range_key, scenario_id)
    return AccountReturnsOut(
        range=range_key,
        period_start=total.period_start,
        period_end=total.period_end,
        accounts=rows,
        total_balance=sum((r.balance for r in rows), ZERO).quantize(CENT),
        total_investment_returns=total.investment_returns,
        total_rate_of_return_pct=total.rate_of_return_pct,
    )


# ------------------------------------------------------------- performance

def performance(db: Session, user: User, account_id: str | None, range_key: str,
                scenario_id: str | None = None) -> PerformanceOut:
    accounts = _user_accounts(db, user, account_id, scenario_id)
    ids = [a.id for a in accounts]
    if not ids:
        return PerformanceOut(series=[])

    txns = list(db.execute(
        select(Transaction)
        .where(Transaction.account_id.in_(ids))
        .order_by(Transaction.as_of, Transaction.executed_at)
    ).scalars())
    flows = list(db.execute(
        select(Contribution)
        .where(Contribution.account_id.in_(ids))
        .order_by(Contribution.timestamp)
    ).scalars())
    dividends = list(db.execute(
        select(Dividend).where(Dividend.account_id.in_(ids))
    ).scalars())
    from app.models import OptionTransaction
    option_txns = list(db.execute(
        select(OptionTransaction).where(OptionTransaction.account_id.in_(ids))
    ).scalars())

    # The ledger is stamped in UTC (Transaction.as_of, Contribution.timestamp),
    # so the replay clock has to be UTC too — using the local date dropped every
    # deposit and fill made after 7pm Central from the series.
    today = utcnow().date()
    events_start = min(
        [t.as_of for t in txns] + [f.timestamp.date() for f in flows]
        + [t.as_of for t in option_txns],
        default=None,
    )
    if events_start is None:
        return PerformanceOut(series=[])

    days_back = RANGE_DAYS.get(range_key, RANGE_DAYS["1y"])
    range_start = events_start if days_back is None else max(events_start, today - timedelta(days=days_back))
    range_start = max(range_start, EPOCH)

    tickers = sorted({t.ticker for t in txns})
    price_maps: dict[str, dict[date, Decimal]] = {}
    for ticker in tickers:
        candles, _ = market_data.history(ticker, events_start - timedelta(days=10), today)
        price_maps[ticker] = dict(candles)

    live_prices: dict[str, Decimal] = {}

    def px(ticker: str, d: date, last: dict[str, Decimal]) -> Decimal:
        p = price_maps[ticker].get(d)
        if p is not None:
            last[ticker] = p
        if d == today:  # final point uses live quotes so it matches the summary
            if ticker not in live_prices:
                try:
                    live_prices[ticker] = market_data.quote(ticker).price
                except MarketDataError:
                    live_prices[ticker] = last.get(ticker, ZERO)
            return live_prices[ticker]
        return last.get(ticker, ZERO)

    # Bucket ledger events by day. A backtested fill (as_of before its actual
    # execution) enters the timeline as an in-kind EXTERNAL flow on as_of
    # (shares appear, worth their cost) with the matching cash effect — also an
    # external flow — on the day it was actually executed. That keeps the
    # replayed series free of phantom negative cash, and TWR/IRR treat both
    # sides as flows rather than performance.
    flow_by_day: dict[date, Decimal] = defaultdict(Decimal)   # external flows
    cash_by_day: dict[date, Decimal] = defaultdict(Decimal)
    shares_by_day: dict[date, list[tuple[str, Decimal]]] = defaultdict(list)

    for f in flows:
        amt = Decimal(f.amount)
        d0 = f.timestamp.date()
        cash_by_day[d0] += amt
        flow_by_day[d0] += amt

    # dividends and option premiums/settlements are income (performance), not external flows
    for dv in dividends:
        cash_by_day[dv.event_date] += Decimal(dv.amount)
    for ot in option_txns:
        cash_by_day[ot.as_of] += Decimal(ot.cash_effect)

    for t in txns:
        s = Decimal(t.shares_filled)
        gross = Decimal(t.gross_amount)
        fee = Decimal(t.fees)
        exec_d = t.executed_at.date()
        if t.side == OrderSide.BUY:
            cash_delta, share_delta = -(gross + fee), s
        else:
            cash_delta, share_delta = gross - fee, -s
        shares_by_day[t.as_of].append((t.ticker, share_delta))
        if t.as_of < exec_d:  # backtest
            cash_by_day[exec_d] += cash_delta
            flow_by_day[t.as_of] += -cash_delta
            flow_by_day[exec_d] += cash_delta
        else:
            cash_by_day[t.as_of] += cash_delta

    # Anchor the replay to what the accounts actually hold: any cash the ledger
    # cannot explain (rows predating a schema change, an out-of-band adjustment)
    # enters as an external transfer on the final day, so the chart ends on the
    # real balance without the gap masquerading as investment performance.
    replayed_cash = sum((v for k, v in cash_by_day.items() if k <= today), ZERO)
    residual = sum((Decimal(a.settlement_balance) for a in accounts), ZERO) - replayed_cash
    if residual:
        cash_by_day[today] += residual
        flow_by_day[today] += residual

    cash = ZERO
    shares: dict[str, Decimal] = defaultdict(Decimal)
    last_px: dict[str, Decimal] = {}
    series: list[PerformancePointOut] = []
    pending_flow = ZERO  # flows landing on non-valued (weekend) days roll forward
    twr_product = 1.0
    twr_started = False
    prev_value: Decimal | None = None
    # Everything below is scoped to the selected range: the replay still starts
    # at inception (it has to, to know what is held), but the reported figures
    # cover only [range_start, today] so they change with the timeframe picker.
    beginning_balance = ZERO       # value carried into the range
    period_flows: list[tuple[date, Decimal]] = []
    period_flow_total = ZERO

    d = events_start
    while d <= today:
        cash += cash_by_day.get(d, ZERO)
        for tk, ds in shares_by_day.get(d, ()):
            shares[tk] += ds
        day_flow = flow_by_day.get(d, ZERO)
        pending_flow += day_flow
        if d >= range_start and day_flow:
            period_flows.append((d, day_flow))
            period_flow_total += day_flow

        if d.weekday() < 5 or d == today:
            value = cash + sum((sh * px(tk, d, last_px) for tk, sh in shares.items() if sh), ZERO)
            if d == today and option_txns:
                from app.services.options import positions_value as _opt_value
                value += _opt_value(db, ids)
            if d < range_start:
                beginning_balance = value  # last valuation before the window opens
            elif prev_value is not None and prev_value > CENT:
                twr_product *= float((value - pending_flow) / prev_value)
                twr_started = True
            prev_value = value if value > CENT else None
            pending_flow = ZERO
            if d >= range_start:
                series.append(PerformancePointOut(
                    date=d,
                    value=value.quantize(CENT),
                    # rebased to the window: the gap between this line and the
                    # value line IS the period's investment return
                    net_deposits=(beginning_balance + period_flow_total).quantize(CENT),
                ))
        d += timedelta(days=1)

    ending_balance = series[-1].value if series else ZERO
    investment_returns = ending_balance - beginning_balance - period_flow_total
    period_start = series[0].date if series else None
    period_days = (today - period_start).days if period_start else 0

    twr_pct = (twr_product - 1.0) * 100 if twr_started and len(series) > 1 else None
    irr = _xirr(
        [(period_start or range_start, -beginning_balance)]
        + [(fd, -amt) for fd, amt in period_flows]
        + [(today, ending_balance)]
    )
    irr_pct = irr * 100 if irr is not None else None

    # Vanguard-style "rate of return" is money-weighted. Over a year or more it
    # is annualized (the IRR); over a shorter window annualizing would inflate
    # it, so the period return (Modified Dietz) is reported instead.
    annualized = period_days > 366
    if annualized:
        rate_of_return = irr_pct
    else:
        dietz = _modified_dietz(beginning_balance, period_flows, ending_balance,
                                period_start, today)
        rate_of_return = dietz * 100 if dietz is not None else None

    period_dividends = sum(
        (Decimal(dv.amount) for dv in dividends if dv.event_date >= range_start), ZERO
    )

    return PerformanceOut(
        series=series,
        twr_pct=twr_pct,
        irr_pct=irr_pct,
        rate_of_return_pct=rate_of_return,
        rate_of_return_annualized=annualized,
        beginning_balance=beginning_balance.quantize(CENT),
        ending_balance=ending_balance.quantize(CENT),
        net_cash_flow=period_flow_total.quantize(CENT),
        investment_returns=investment_returns.quantize(CENT),
        dividends=period_dividends.quantize(CENT),
        period_start=period_start,
        period_end=series[-1].date if series else None,
    )


def _modified_dietz(begin: Decimal, flows: list[tuple[date, Decimal]], end: Decimal,
                    start: date | None, finish: date | None) -> float | None:
    """Money-weighted return over a single period, weighting each external flow
    by the fraction of the period it was invested."""
    if start is None or finish is None:
        return None
    span = (finish - start).days
    if span <= 0:
        return None
    weighted = 0.0
    total_flow = 0.0
    for d, amount in flows:
        w = max(0.0, min(1.0, (finish - d).days / span))
        weighted += float(amount) * w
        total_flow += float(amount)
    denominator = float(begin) + weighted
    if abs(denominator) < 0.01:
        return None
    return (float(end) - float(begin) - total_flow) / denominator


def _xirr(cashflows: Iterable[tuple[date, Decimal]]) -> float | None:
    """Annualized internal rate of return via Newton with bisection fallback."""
    flows = [(d, float(a)) for d, a in cashflows if a != 0]
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)
    times = [((d - t0).days / 365.0, a) for d, a in flows]

    def f(r: float) -> float:
        return sum(a / (1.0 + r) ** t for t, a in times)

    def fprime(r: float) -> float:
        return sum(-t * a / (1.0 + r) ** (t + 1) for t, a in times)

    r = 0.1
    for _ in range(60):
        fr = f(r)
        if abs(fr) < 1e-8:
            return r
        d1 = fprime(r)
        if d1 == 0:
            break
        step = fr / d1
        r_next = r - step
        if r_next <= -0.9999:
            r_next = (r - 0.9999) / 2
        if abs(r_next - r) < 1e-10:
            return r_next
        r = r_next

    lo, hi = -0.9999, 10.0
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-8:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2
