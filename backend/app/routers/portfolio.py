from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import date
from decimal import Decimal

import logging

from app.db import get_db
from app.deps import Principal, owned_account, require_read
from app.models import Account, AccountType, Dividend, TaxLot
from app.rate_limit import rate_limiter
from app.schemas import (
    AccountReturnsOut,
    DividendOut,
    LotOut,
    MonthEventOut,
    MonthlyPerformanceOut,
    PerformanceOut,
    PortfolioSummaryOut,
    PositionOut,
    SettlementOut,
)
from app.services import metrics
from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.portfolio")

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/dividends", response_model=list[DividendOut])
def portfolio_dividends(
    account_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
):
    """Dividend events for the active scenario, newest first. `account_id`
    narrows to one account; `limit` caps the page size."""
    q = (
        select(Dividend)
        .join(Account, Account.id == Dividend.account_id)
        .where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
        .order_by(Dividend.event_date.desc())
        .limit(limit)
    )
    if account_id:
        q = q.where(Dividend.account_id == account_id)
    return [DividendOut.model_validate(d) for d in db.execute(q).scalars()]


def _refresh_dividends(user_id: str) -> None:
    """Ask a worker to bring this user's dividends up to date.

    Off the request path on purpose: the first sweep of the day costs one
    calendar lookup per security held, and nobody should wait on that to see
    their dashboard. It is also throttled to once per account per day, so the
    enqueue is a no-op on every load after the first.

    Best-effort by design — if the broker is unreachable the page still
    renders. Nothing is lost, because reconciliation is a pure function of the
    ledger and simply runs on the next read or before the next order.
    """
    try:
        from app.workers.tasks import reconcile_user_dividends

        reconcile_user_dividends.delay(user_id)
    except Exception:                                    # noqa: BLE001
        log.debug("could not enqueue dividend refresh", exc_info=True)


@router.get("/summary", response_model=PortfolioSummaryOut)
def portfolio_summary(account_id: str | None = None,
                      principal: Principal = Depends(require_read),
                      db: Session = Depends(get_db)) -> PortfolioSummaryOut:
    """Balances and gains across the active scenario (or one account): cost
    basis, unrealized gains, realized gains split into taxable and
    tax-sheltered portions, total dividends and fees, and cash reserved or
    committed to open orders.

    Reading this is what makes a user's dividends current: the first call of
    the day schedules a reconciliation in the background."""
    _refresh_dividends(principal.user.id)
    return metrics.summary(db, principal.user, account_id, principal.scenario_id)


@router.get("/positions", response_model=list[PositionOut])
def portfolio_positions(account_id: str | None = None,
                        principal: Principal = Depends(require_read),
                        db: Session = Depends(get_db)):
    """Open positions across the active scenario (or one account), priced at
    the current live quote with unrealized gains against average cost."""
    return metrics.positions_view(db, principal.user, account_id, principal.scenario_id)


@router.get("/lots", response_model=list[LotOut])
def portfolio_lots(account_id: str, ticker: str | None = None,
                   principal: Principal = Depends(require_read),
                   db: Session = Depends(get_db)):
    """Individual tax lots for one account, each priced at the current quote
    with its holding-period term (LONG once held over a year) and unrealized
    gain. Narrow to one ticker with `ticker`."""
    owned_account(account_id, principal, db)
    q = select(TaxLot).where(TaxLot.account_id == account_id).order_by(TaxLot.acquired_on)
    if ticker:
        q = q.where(TaxLot.ticker == ticker.upper())
    out: list[LotOut] = []
    prices: dict[str, Decimal | None] = {}
    for lot in db.execute(q).scalars():
        if lot.ticker not in prices:
            try:
                prices[lot.ticker] = market_data.quote(lot.ticker).price
            except MarketDataError:
                prices[lot.ticker] = None
        price = prices[lot.ticker]
        shares = Decimal(lot.shares_open)
        cost = Decimal(lot.cost_per_share)
        out.append(LotOut(
            id=lot.id,
            account_id=lot.account_id,
            ticker=lot.ticker,
            shares_open=shares,
            cost_per_share=cost,
            cost_basis=(shares * cost).quantize(Decimal("0.01")),
            acquired_on=lot.acquired_on,
            term="LONG" if (date.today() - lot.acquired_on).days > 365 else "SHORT",
            price=price,
            unrealized_gains=((price - cost) * shares).quantize(Decimal("0.01")) if price else None,
        ))
    return out


@router.get("/returns", response_model=AccountReturnsOut,
            dependencies=[Depends(rate_limiter("performance", 30, 60))])
def account_returns(
    range: str = Query(default="1y", pattern="^(1m|3m|6m|1y|3y|5y|10y|all)$"),
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> AccountReturnsOut:
    """Balance, investment returns and rate of return per account, scoped to
    the requested timeframe."""
    return metrics.account_returns(db, principal.user, range, principal.scenario_id)


@router.get("/settlement", response_model=list[SettlementOut])
def settlement_funds(account_id: str | None = None,
                     principal: Principal = Depends(require_read),
                     db: Session = Depends(get_db)):
    """The settlement fund (VMFXX) position behind each account's cash."""
    from app.services import settlement as svc

    q = select(Account).where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id).order_by(Account.created_at)
    if account_id:
        q = q.where(Account.id == account_id)
    return [
        SettlementOut(account_id=a.id, account_name=a.name, **svc.holding_view(a))
        for a in db.execute(q).scalars()
    ]


@router.get("/performance", response_model=PerformanceOut,
            dependencies=[Depends(rate_limiter("performance", 30, 60))])
def portfolio_performance(
    account_id: str | None = None,
    range: str = Query(default="1y", pattern="^(1m|3m|6m|1y|3y|5y|10y|all)$"),
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> PerformanceOut:
    """Value series and returns for the active scenario (or one account) over
    the requested range. Rate of return is Modified Dietz under a year and
    annualized IRR (XIRR) beyond a year; TWR is reported alongside either way."""
    return metrics.performance(db, principal.user, account_id, range, principal.scenario_id)


@router.get("/performance/monthly", response_model=MonthlyPerformanceOut,
            dependencies=[Depends(rate_limiter("performance", 30, 60))])
def portfolio_performance_monthly(
    account_id: str | None = None,
    account_type: AccountType | None = None,
    months: int | None = Query(default=12, ge=1, le=600),
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> MonthlyPerformanceOut:
    """Month-by-month performance for the active scenario, newest first.

    Each row splits the change in value into what was put in (`net_cash_flow`),
    what the market did (`market_gain`), and what was paid out (`income`), so

        ending_balance = beginning_balance + net_cash_flow + market_gain + income

    `cumulative_return` runs from inception regardless of how many months are
    requested — asking to see fewer months does not change how much of the
    balance the market provided. Scope with `account_id` for a single account or
    `account_type` for every account of one kind; omit both to aggregate.

    `months` caps how many rows come back (default 12); pass a larger number or
    omit it for the full history."""
    return metrics.monthly_performance(db, principal.user, account_id, months,
                                       principal.scenario_id, account_type)


@router.get("/performance/monthly/{month}/events", response_model=list[MonthEventOut],
            dependencies=[Depends(rate_limiter("performance", 30, 60))])
def portfolio_month_events(
    month: str = Path(pattern=r"^\d{4}-\d{2}$", description="YYYY-MM"),
    account_id: str | None = None,
    account_type: AccountType | None = None,
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> list[MonthEventOut]:
    """Everything that happened inside one month — deposits, withdrawals, fills
    and dividends — in date order, scoped the same way as the monthly table.

    This is what sits behind a row: the daily *shape* of a month is already the
    performance chart, so what is useful here is which events produced it."""
    return metrics.month_events(db, principal.user, month, account_id,
                                principal.scenario_id, account_type)
