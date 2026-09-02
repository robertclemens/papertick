"""Trading engine.

Execution paths for an order:
  - market order, market open      -> fill at live quote with configured slippage
  - market order, market closed    -> queued (SCHEDULED) for the next NYSE open
  - mutual fund order              -> forward-priced: fills at that day's closing
                                      NAV (4:00 PM ET cutoff), like a real fund
  - historical (as_of)             -> fill at that date's split-adjusted close
  - scheduled_for future           -> executed by the worker at/after that time
  - limit order                    -> PENDING until the market crosses the price
                                      (checked only during market hours)

Cost basis is tracked as tax lots: buys open a lot (fees in basis, acquired on
the fill's effective date — so backtests earn their holding period); sells
consume lots FIFO and split realized gains into short/long-term (>1y). Position
rows are aggregates over open lots. All mutations run under SELECT ... FOR
UPDATE row locks; fills are all-or-nothing. ENFORCE_MARKET_HOURS=false turns
off the market-clock emulation (everything fills instantly at the last price).
"""

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import for_update

from app.config import get_settings
from app.models import (
    Account,
    AccountType,
    Asset,
    AssetCategory,
    AssetClass,
    AssetRegion,
    CostBasisMethod,
    CostBasisOverride,
    OPEN_STATUSES,
    OptionPosition,
    Order,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    Position,
    QuantityType,
    TaxLot,
    TimeInForce,
    Transaction,
    User,
    utcnow,
)
from app.schemas import OrderCreateIn
from app.services import market_calendar as cal
from app.services.market_data import EPOCH, MarketDataError, market_data

log = logging.getLogger("papertick.trading")

CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
ZERO = Decimal("0")
LONG_TERM_DAYS = 365


def q_money(v: Decimal) -> Decimal:
    return v.quantize(CENT, ROUND_HALF_UP)


def q_shares(v: Decimal) -> Decimal:
    return v.quantize(MICRO, ROUND_DOWN)


def q_price(v: Decimal) -> Decimal:
    return v.quantize(MICRO, ROUND_HALF_UP)


def _slippage_bps(seed: str | None) -> Decimal:
    """Basis points of slippage for one fill.

    `fixed` returns SLIPPAGE_BPS flat. `variable` draws from a triangular
    distribution over [MIN, MODE, MAX]: most fills land near the typical
    spread, the adverse tail is longer than the favourable one, and a negative
    draw represents the price improvement real retail flow does receive. A
    single static value is the one thing real fills never look like.

    The draw is seeded from the order id rather than a PRNG, so it is stable:
    re-running the same backtest, or replaying a missed recurring buy after an
    outage, reproduces the fill instead of inventing a new one. With no seed
    (a preview) the mode is returned, so a quote shown before the trade is the
    expected cost rather than a number that moves on refresh.
    """
    s = get_settings()
    mode = Decimal(s.slippage_bps)
    if s.slippage_model == "fixed" or seed is None:
        return mode
    low, high = Decimal(s.slippage_bps_min), Decimal(s.slippage_bps_max)
    if high <= low:
        return mode
    mode = min(max(mode, low), high)

    # sha256 -> u in (0,1), the same deterministic-uniform trick the synthetic
    # provider uses, so nothing here depends on process-local RNG state
    digest = hashlib.sha256(f"slip|{seed}".encode()).digest()
    u = Decimal(int.from_bytes(digest[:8], "big") + 1) / Decimal(2**64 + 2)

    span, lower = high - low, mode - low
    pivot = lower / span
    if u < pivot:
        draw = low + (span * lower * u).sqrt()
    else:
        draw = high - (span * (high - mode) * (Decimal(1) - u)).sqrt()
    return draw


def _slipped(price: Decimal, side: OrderSide, seed: str | None = None) -> Decimal:
    bps = _slippage_bps(seed)
    factor = Decimal(1) + (bps / Decimal(10000)) * (1 if side == OrderSide.BUY else -1)
    return q_price(price * factor)


def reserved_cash(db: Session, account_id: str) -> Decimal:
    """Cash reserved as collateral for short puts. It stays in settlement_balance
    but is unavailable for purchases and withdrawals."""
    total = db.execute(
        select(func.coalesce(func.sum(OptionPosition.collateral), 0))
        .where(OptionPosition.account_id == account_id)
    ).scalar_one()
    return Decimal(total)


def _estimated_cost(db: Session, order: Order) -> Decimal:
    """What an open BUY order is expected to consume in cash."""
    if order.quantity_type == QuantityType.DOLLARS:
        return q_money(Decimal(order.quantity))
    price = Decimal(order.limit_price) if order.limit_price else None
    if price is None:
        try:
            price = market_data.quote(order.ticker).price
        except MarketDataError:
            price = ZERO
    return q_money(Decimal(order.quantity) * price)


def committed_cash(db: Session, account_id: str, exclude_order_id: str | None = None) -> Decimal:
    """Cash already earmarked by open BUY orders (queued for the next open,
    awaiting a NAV print, or resting limit orders). Without this, the same
    dollars could be committed to several pending orders at once."""
    open_orders = db.execute(
        select(Order).where(
            Order.account_id == account_id,
            Order.side == OrderSide.BUY,
            Order.status.in_(OPEN_STATUSES),
        )
    ).scalars().all()
    return sum(
        (_estimated_cost(db, o) for o in open_orders if o.id != exclude_order_id),
        ZERO,
    )


def committed_shares(db: Session, account_id: str, ticker: str,
                     exclude_order_id: str | None = None) -> Decimal:
    """Shares already earmarked by open SELL orders — the mirror of committed
    cash, so the same shares cannot back two resting sell orders."""
    open_orders = db.execute(
        select(Order).where(
            Order.account_id == account_id,
            Order.ticker == ticker,
            Order.side == OrderSide.SELL,
            Order.status.in_(OPEN_STATUSES),
        )
    ).scalars().all()
    total = ZERO
    for o in open_orders:
        if o.id == exclude_order_id:
            continue
        if o.quantity_type == QuantityType.SHARES:
            total += Decimal(o.quantity)
        else:
            price = Decimal(o.limit_price) if o.limit_price else None
            if price is None:
                try:
                    price = market_data.quote(o.ticker).price
                except MarketDataError:
                    continue
            if price > 0:
                total += q_shares(Decimal(o.quantity) / price)
    return total


def sellable_shares(db: Session, account_id: str, ticker: str,
                    exclude_order_id: str | None = None) -> Decimal:
    """Shares held minus those committed to open sell orders."""
    position = db.execute(
        select(Position).where(Position.account_id == account_id, Position.ticker == ticker)
    ).scalar_one_or_none()
    held = Decimal(position.shares) if position else ZERO
    return held - committed_shares(db, account_id, ticker, exclude_order_id)


def buying_power(db: Session, account_id: str, exclude_order_id: str | None = None) -> Decimal:
    """Cash actually available to trade: balance minus short-put collateral
    minus cash already committed to open buy orders."""
    account = db.get(Account, account_id)
    if account is None:
        return ZERO
    return (
        Decimal(account.settlement_balance)
        - reserved_cash(db, account_id)
        - committed_cash(db, account_id, exclude_order_id)
    )


def account_out(db: Session, account: Account):
    """AccountOut enriched with live buying power and settlement-fund detail
    (the single place that computes them, so every response agrees)."""
    from app.schemas import AccountOut
    from app.services import irs, settlement

    out = AccountOut.model_validate(account)
    out.buying_power = q_money(buying_power(db, account.id))
    out.settlement_ticker = settlement.TICKER
    out.settlement_name = settlement.NAME
    out.settlement_yield = settlement.current_yield()
    out.settlement_accrued = q_money(Decimal(account.settlement_accrued or 0))
    out.contribution_statuses = irs.contribution_statuses(db, account)
    out.backdated_fills = db.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.account_id == account.id, Transaction.backdated.is_(True)
        )
    ).scalar_one()
    return out


class FundingError(Exception):
    """Raised when an external transfer cannot cover a shortfall."""


def fundable_amount(db: Session, account: Account) -> Decimal:
    """How much external cash may still be pulled into this account.

    Every purchase is paid for out of the settlement fund, and a purchase the
    fund cannot cover pulls in what it is short — the platform has no view of
    what you hold elsewhere, so it takes the order as a statement that the
    money exists. Taxable accounts are therefore unconstrained. The two limits
    that remain are legal ones the IRS imposes, not preferences: an IRA is
    capped by the room left across every open contribution bucket (between
    Jan 1 and Tax Day that is the prior year plus the current one), and a
    Rollover IRA accepts no regular contribution at all."""
    from app.services import irs

    if account.account_type == AccountType.TAXABLE:
        return Decimal("10000000")
    if account.account_type == AccountType.ROLLOVER_IRA:
        # pulling cash in would be a regular contribution, which a rollover
        # account does not accept
        return ZERO
    user = db.get(User, account.user_id)
    if user is None:
        return ZERO
    return sum((room for _, room, _ in irs.open_tax_years(db, user, None,
                                                          account.scenario_id)), ZERO)


def auto_fund(db: Session, account: Account, shortfall: Decimal, memo: str) -> Decimal:
    """Pull `shortfall` from an external bank into `account`, recording it as a
    cash transfer (and, in an IRA, as a contribution for the current tax year
    so it counts against the limit). Raises FundingError only where the IRS
    forbids the transfer. Caller commits."""
    from app.models import CashFlowKind, Contribution
    from app.services import irs

    if shortfall <= 0:
        return ZERO
    if account.account_type == AccountType.ROLLOVER_IRA:
        raise FundingError(
            "A Rollover IRA takes rollover money only, so a bank transfer cannot "
            "cover this order — sell something or roll funds in first"
        )
    # Same rule as a manual deposit: the shared IRA limit is checked and
    # consumed in one atomic step, so lock every account it spans first.
    if account.account_type in irs.IRA_LIKE:
        irs.lock_contribution_scope(db, db.get(User, account.user_id), account.scenario_id)
    available = fundable_amount(db, account)
    if available < shortfall:
        if account.account_type == AccountType.TAXABLE:
            raise FundingError("External transfer unavailable")
        raise FundingError(
            f"An external transfer of ${shortfall} would exceed your "
            f"{date.today().year} IRA contribution limit "
            f"(${available} of contribution room left)"
        )
    amount = q_money(shortfall)
    if account.account_type == AccountType.TAXABLE:
        buckets: list[tuple[int | None, Decimal]] = [(None, amount)]
    else:
        # fill the oldest open bucket first: prior-year room lapses at Tax Day,
        # this year's does not
        from app.services import irs

        buckets = []
        left = amount
        user = db.get(User, account.user_id)
        for year, room, _ in irs.open_tax_years(db, user, None, account.scenario_id):
            if left <= 0:
                break
            take = min(left, room)
            if take > 0:
                buckets.append((year, q_money(take)))
                left -= take
    for tax_year, part in buckets:
        db.add(Contribution(
            account_id=account.id,
            tax_year=tax_year,
            amount=part,
            kind=CashFlowKind.CONTRIBUTION,
            memo=memo[:200],
        ))
    account.settlement_balance = Decimal(account.settlement_balance) + amount
    log.info("auto-funded %s with $%s (%s)", account.id, amount, memo)
    return amount


def resolve_cost_basis_method(db: Session, account: Account, ticker: str,
                              order_override: CostBasisMethod | None) -> CostBasisMethod:
    if order_override is not None:
        return order_override
    override = db.execute(
        select(CostBasisOverride).where(
            CostBasisOverride.account_id == account.id, CostBasisOverride.ticker == ticker
        )
    ).scalar_one_or_none()
    if override is not None:
        return override.method
    return account.cost_basis_method or CostBasisMethod.FIFO


def _lot_term_days(lot: TaxLot, as_of: date) -> int:
    return (as_of - lot.acquired_on).days


def _consumption_plan(
    order: Order,
    method: CostBasisMethod,
    eligible: list[TaxLot],
    shares: Decimal,
    price: Decimal,
    as_of: date,
) -> list[tuple[TaxLot, Decimal]] | str:
    """Which lots a sale consumes, in order. Returns an error string on failure."""
    if method == CostBasisMethod.SPEC_ID:
        try:
            requested = json.loads(order.spec_lots)
        except (TypeError, ValueError):
            return "Invalid lot selection"
        by_id = {l.id: l for l in eligible}
        plan: list[tuple[TaxLot, Decimal]] = []
        total = ZERO
        for item in requested:
            lot = by_id.get(str(item.get("lot_id")))
            take = Decimal(str(item.get("shares", 0)))
            if lot is None:
                return f"Lot {item.get('lot_id')} is not an open lot of {order.ticker} in this account"
            if take <= 0 or take > Decimal(lot.shares_open):
                return f"Lot {lot.id[:8]} holds {lot.shares_open} shares; cannot sell {take} from it"
            plan.append((lot, take))
            total += take
        if total != shares:
            return f"Selected lots cover {total} shares but the order sells {shares}"
        return plan

    if method == CostBasisMethod.LIFO:
        ordered = sorted(eligible, key=lambda l: (l.acquired_on, l.created_at), reverse=True)
    elif method == CostBasisMethod.HIFO:
        ordered = sorted(eligible, key=lambda l: Decimal(l.cost_per_share), reverse=True)
    elif method == CostBasisMethod.MIN_TAX:
        def rank(l: TaxLot) -> tuple:
            gain = price - Decimal(l.cost_per_share)
            long_term = _lot_term_days(l, as_of) > LONG_TERM_DAYS
            if gain < 0:
                r = 0 if not long_term else 1   # ST losses, then LT losses
            else:
                r = 2 if long_term else 3       # LT gains, then ST gains
            return (r, -Decimal(l.cost_per_share))
        ordered = sorted(eligible, key=rank)
    else:  # FIFO and AVERAGE (average uses FIFO ordering for holding period)
        ordered = sorted(eligible, key=lambda l: (l.acquired_on, l.created_at))

    plan = []
    remaining = shares
    for lot in ordered:
        if remaining <= 0:
            break
        take = min(Decimal(lot.shares_open), remaining)
        plan.append((lot, take))
        remaining -= take
    return plan


_ITYPE_MAP = {
    "EQUITY": (AssetClass.EQUITY, AssetCategory.STOCK, AssetRegion.US),
    "ETF": (AssetClass.ETF, AssetCategory.OTHER, AssetRegion.OTHER),
    "MUTUALFUND": (AssetClass.MUTUAL_FUND, AssetCategory.OTHER, AssetRegion.OTHER),
}


def valid_ticker(ticker: str) -> str:
    """Normalise and validate a symbol, or raise 422.

    The value reaches an upstream provider's URL path and a Redis cache key, so
    it is checked once, here, before any lookup — an unbounded path parameter
    would otherwise let a caller reshape the provider request (`?`, `#`, `../`)
    or wedge a NUL byte into a Postgres text column.
    """
    from app.schemas import _ticker

    try:
        return _ticker(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Ticker {exc}")


def require_asset(db: Session, ticker: str) -> Asset:
    """Return the asset, auto-registering any USD-denominated US-listed symbol
    the live data source recognizes (curated seed assets carry real category /
    region / prospectus metadata; auto-registered ones default to OTHER)."""
    ticker = valid_ticker(ticker)
    asset = db.get(Asset, ticker)
    if asset is not None:
        return asset
    info = market_data.lookup_symbol(ticker)
    if info is not None and info.currency == "USD" and info.instrument_type in _ITYPE_MAP:
        klass, category, region = _ITYPE_MAP[info.instrument_type]
        asset = Asset(
            ticker=ticker,
            name=info.name[:120],
            asset_class=klass,
            category=category,
            region=region,
            prospectus_url=(
                f"https://www.sec.gov/edgar/search/#/q=%22{ticker}%22&forms=485BPOS"
                if klass != AssetClass.EQUITY else None
            ),
            auto_registered=True,
        )
        db.add(asset)
        db.flush()
        log.info("auto-registered asset %s (%s)", ticker, info.instrument_type)
        return asset
    raise HTTPException(
        status_code=422,
        detail=(
            f"Unknown ticker {ticker!r}. It could not be validated as a US-listed "
            "USD equity, ETF or mutual fund (see /api/v1/market/assets for the "
            "known universe)."
        ),
    )


BACKDATE_REFUSED = (
    "Past-dated trades are off for this scenario. They rewrite periods that "
    "already have statements and let an order be placed knowing the outcome, so "
    "they are opt-in per scenario — turn them on in the scenario's settings, or "
    "switch to a scenario that allows them. Everything a scenario produces while "
    "they are on is marked as containing past-dated fills."
)


def backdating_allowed(db: Session, account: Account) -> bool:
    """Whether this account's scenario accepts past-dated ("as of") fills.

    A scenario is exactly the right place to test a hypothesis whose outcome is
    already known — provided the scenario opted in and everything it produces
    says so. That is why this is a per-scenario setting rather than one switch
    for the whole deployment.
    """
    from app.models import Scenario

    scenario = db.get(Scenario, account.scenario_id)
    return bool(scenario and scenario.allow_backdated)


def place_order(db: Session, account: Account, data: OrderCreateIn, source: OrderSource,
                now: datetime | None = None,
                exchange_to: str | None = None) -> tuple[Order, Transaction | None]:
    # This price is about to become a permanent ledger row, so the provider it
    # comes from must have been verified on the right adjustment basis
    # recently enough to trust. The common case is a cached verdict: one
    # 0.13ms Redis read. Only an expired one pays to re-measure.
    from app.services.convention import ensure_fresh_for_write

    ensure_fresh_for_write()
    reject_settlement_ticker(data.ticker)
    asset = require_asset(db, data.ticker)
    now = now or utcnow()
    today = now.date()
    enforce = get_settings().enforce_market_hours
    is_mf = asset.asset_class == AssetClass.MUTUAL_FUND

    if data.as_of is not None and data.scheduled_for is not None:
        raise HTTPException(status_code=422, detail="Use either as_of (backtest) or scheduled_for, not both")
    if data.as_of is not None:
        if not backdating_allowed(db, account):
            raise HTTPException(status_code=422, detail=BACKDATE_REFUSED)
        if data.order_type != OrderType.MARKET:
            raise HTTPException(status_code=422, detail="Historical (as_of) orders must be MARKET orders")
        if data.as_of >= today:
            raise HTTPException(status_code=422, detail="as_of must be a past date")
        if data.as_of < EPOCH:
            raise HTTPException(status_code=422, detail=f"as_of cannot be before {EPOCH.isoformat()}")
    if data.scheduled_for is not None:
        sched = data.scheduled_for
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
            data = data.model_copy(update={"scheduled_for": sched})
        if sched <= now:
            raise HTTPException(status_code=422, detail="scheduled_for must be in the future")
        if sched > now + timedelta(days=366):
            raise HTTPException(status_code=422, detail="scheduled_for must be within one year")
    expires_at: datetime | None = None
    tif: TimeInForce | None = None
    if data.order_type == OrderType.LIMIT:
        if is_mf:
            raise HTTPException(status_code=422, detail="Mutual funds trade once daily at NAV; limit orders are not supported")
        if data.limit_price is None:
            raise HTTPException(status_code=422, detail="limit_price is required for LIMIT orders")
        if data.scheduled_for is not None:
            raise HTTPException(status_code=422, detail="LIMIT orders cannot be scheduled")
        tif = data.time_in_force or TimeInForce.GTC_60
        expires_at = expiry_for(tif, now)

    if data.side == OrderSide.BUY and (data.cost_basis_method is not None or data.spec_lots):
        raise HTTPException(status_code=422, detail="Cost-basis selection applies to sells only")
    if (data.cost_basis_method is not None or data.spec_lots) and \
            account.account_type.value != "TAXABLE":
        raise HTTPException(
            status_code=422,
            detail="Cost-basis elections apply only to taxable brokerage accounts — "
                   "IRA sales have no capital-gains treatment and use FIFO",
        )
    if data.spec_lots:
        if data.cost_basis_method not in (None, CostBasisMethod.SPEC_ID):
            raise HTTPException(status_code=422, detail="Lot selection requires the SPEC_ID cost-basis method")
        if data.quantity_type != QuantityType.SHARES:
            raise HTTPException(status_code=422, detail="Specific-lot sales must be entered in shares")
        total_spec = sum((l.shares for l in data.spec_lots), Decimal(0))
        if total_spec != data.quantity:
            raise HTTPException(
                status_code=422,
                detail=f"Selected lots cover {total_spec} shares but the order sells {data.quantity}",
            )
    if data.cost_basis_method == CostBasisMethod.SPEC_ID and not data.spec_lots:
        raise HTTPException(status_code=422, detail="SPEC_ID requires a lot selection (spec_lots)")

    funding_note: str | None = None
    if data.side == OrderSide.BUY:
        # raises 422 when the account is short and cannot be funded
        funding_note = _preflight_buying_power(db, account, data, now)
    else:
        _preflight_shares(db, account, data)

    order = Order(
        account_id=account.id,
        ticker=data.ticker,
        side=data.side,
        order_type=data.order_type,
        quantity_type=data.quantity_type,
        quantity=data.quantity,
        limit_price=data.limit_price if data.order_type == OrderType.LIMIT else None,
        time_in_force=tif,
        expires_at=expires_at,
        scheduled_for=data.scheduled_for,
        as_of=data.as_of,
        cost_basis_method=(
            CostBasisMethod.SPEC_ID if data.spec_lots else data.cost_basis_method
        ),
        spec_lots=(
            json.dumps([{"lot_id": l.lot_id, "shares": str(l.shares)} for l in data.spec_lots])
            if data.spec_lots else None
        ),
        source=source,
        exchange_to_ticker=exchange_to,
    )
    db.add(order)
    # transient annotation for the API response; never persisted
    order.funding_note = funding_note

    # user-scheduled future execution: the worker picks it up at that time
    if data.scheduled_for is not None:
        order.status = OrderStatus.SCHEDULED
        db.commit()
        return order, None

    if data.order_type == OrderType.LIMIT:
        order.status = OrderStatus.PENDING
        db.flush()
        txn = None
        if not enforce or cal.is_market_open(now):
            txn = try_fill_limit_order(db, order)
        db.commit()
        return order, txn

    # historical (backtest) — fills at the historical close/NAV immediately
    if data.as_of is not None:
        price = _close_or_none(order.ticker, data.as_of)
        if price is None:
            return _reject(db, order, f"No market data for {order.ticker} on {data.as_of}")
        txn = execute_fill(db, order, price, data.as_of)
        if txn is not None:
            _post_backtest_dividends(db, order)
            _restate_after_backdated_fill(db, account, data.as_of)
        db.commit()
        return order, txn

    # mutual fund: forward pricing at the daily closing NAV
    if is_mf and enforce:
        order.nav_date = cal.nav_date_for(now)
        order.scheduled_for = cal.mf_fill_time(order.nav_date)
        order.status = OrderStatus.SCHEDULED
        db.commit()
        return order, None

    # market closed: queue for the next NYSE open, like a real brokerage
    if enforce and not cal.is_market_open(now):
        order.status = OrderStatus.SCHEDULED
        order.scheduled_for = cal.next_market_open(now)
        db.commit()
        return order, None

    try:
        quote = market_data.quote(order.ticker)
    except MarketDataError as exc:
        return _reject(db, order, f"Market data unavailable: {exc}")
    txn = execute_fill(db, order, _slipped(quote.price, order.side, order.id), today)
    db.commit()
    return order, txn


def _post_backtest_dividends(db: Session, order: Order) -> None:
    """Credit the dividends a backdated position would have earned since as_of."""
    from app.services.dividends import reconcile_account_ticker

    try:
        reconcile_account_ticker(db, order.account_id, order.ticker)
    except Exception:  # dividend backfill must never break the fill itself
        log.exception("dividend backfill failed for %s/%s", order.account_id, order.ticker)


def _restate_after_backdated_fill(db: Session, account: Account, as_of: date) -> None:
    """A past-dated fill changes history, so every statement covering that date
    onward is re-rendered from the corrected ledger."""
    from app.services.statements import regenerate_from

    user = db.get(User, account.user_id)
    if user is None:
        return
    try:
        regenerate_from(db, user, as_of, scenario_id=account.scenario_id)
    except Exception:  # restatement must never break the fill itself
        log.exception("statement restatement failed for %s after %s", user.id, as_of)


def _preflight_buying_power(db: Session, account: Account, data: OrderCreateIn,
                            now: datetime) -> str | None:
    """Check funds BEFORE accepting a buy order, so queued orders earmark their
    cash and a second order cannot spend the same dollars. Pulls an external
    transfer when the account is short and funding is permitted. Returns a note
    describing any transfer made.

    Backtested (as_of) orders skip this: they settle against the historical
    ledger, which execute_fill validates at fill time.
    """
    if data.as_of is not None:
        return None
    fee = q_money(Decimal(get_settings().trade_fee_usd))
    if data.quantity_type == QuantityType.DOLLARS:
        needed = q_money(Decimal(data.quantity)) + fee
    else:
        price = data.limit_price
        if price is None:
            try:
                price = market_data.quote(data.ticker).price
            except MarketDataError:
                return None  # priced at fill time instead
        needed = q_money(Decimal(data.quantity) * Decimal(price)) + fee

    available = buying_power(db, account.id)
    if available >= needed:
        return None

    shortfall = needed - available
    try:
        funded = auto_fund(
            db, account, shortfall,
            f"External bank transfer — funding {data.ticker} order",
        )
    except FundingError as exc:
        committed = committed_cash(db, account.id)
        detail = (
            f"Insufficient buying power: this order needs ${needed} but only "
            f"${q_money(available)} is available to trade"
        )
        if committed > 0:
            detail += f" (${q_money(committed)} is already committed to open orders)"
        raise HTTPException(status_code=422, detail=f"{detail}. {exc}")
    return (
        f"Transferred ${funded} from your external bank to cover this order."
        if funded else None
    )


TIF_DAYS = {
    TimeInForce.GTC_30: 30,
    TimeInForce.GTC_60: 60,
    TimeInForce.GTC_90: 90,
    TimeInForce.GTC_180: 180,
    TimeInForce.GTC: 365,
}


def expiry_for(tif: TimeInForce, now: datetime) -> datetime:
    """When a resting order lapses and releases its committed cash/shares."""
    if tif == TimeInForce.DAY:
        d = now.astimezone(cal.ET).date()
        close = cal.market_close_at(d)
        # placed after today's close (or on a holiday): good through the next session
        return close if cal.is_trading_day(d) and now < close else cal.next_market_close(now)
    return cal.next_market_close(now + timedelta(days=TIF_DAYS[tif]))


def _preflight_shares(db: Session, account: Account, data: OrderCreateIn) -> None:
    """Reject a sell that the account cannot cover once shares already promised
    to other open sell orders are set aside. Backtested sells are validated
    against the historical ledger at fill time instead."""
    if data.as_of is not None:
        return
    available = sellable_shares(db, account.id, data.ticker)
    if data.quantity_type == QuantityType.SHARES:
        wanted = Decimal(data.quantity)
    else:
        price = data.limit_price
        if price is None:
            try:
                price = market_data.quote(data.ticker).price
            except MarketDataError:
                return
        if price <= 0:
            return
        wanted = q_shares(Decimal(data.quantity) / Decimal(price))
    if wanted > available:
        committed = committed_shares(db, account.id, data.ticker)
        detail = (
            f"Insufficient shares: this order sells {wanted} {data.ticker} but only "
            f"{max(available, ZERO)} are available"
        )
        if committed > 0:
            detail += f" ({committed} already committed to open sell orders)"
        raise HTTPException(status_code=422, detail=detail)


def expire_due_orders(db: Session, now: datetime | None = None) -> int:
    """Lapse resting orders whose time-in-force has elapsed, releasing the cash
    or shares they had committed."""
    now = now or utcnow()
    from app.services.scenarios import frozen_accounts

    due = db.execute(
        for_update(
            select(Order).where(
                Order.status == OrderStatus.PENDING,
                Order.expires_at.isnot(None),
                Order.expires_at <= now,
                Order.account_id.notin_(frozen_accounts(db)),
            ),
            skip_locked=True,
        )
    ).scalars().all()
    for order in due:
        order.status = OrderStatus.EXPIRED
        order.reject_reason = (
            f"Time in force ({order.time_in_force.value if order.time_in_force else 'GTC'}) "
            "elapsed before the limit price was reached"
        )
    if due:
        db.commit()
        log.info("expired %d resting order(s)", len(due))
    return len(due)


def _aware(value: datetime | None) -> datetime | None:
    """Stored timestamps are UTC, but not every backend hands them back with a
    tzinfo (SQLite has no tz type). A naive one here would raise mid-comparison
    and take the whole scheduled-order sweep down with it."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _close_or_none(ticker: str, d: date) -> Decimal | None:
    try:
        return market_data.close_on(ticker, d)
    except MarketDataError:
        return None


def _reject(db: Session, order: Order, reason: str) -> tuple[Order, None]:
    order.status = OrderStatus.REJECTED
    order.reject_reason = reason
    db.commit()
    return order, None


def _reject_inline(order: Order, reason: str) -> None:
    order.status = OrderStatus.REJECTED
    order.reject_reason = reason
    return None


def _sync_position(db: Session, account_id: str, ticker: str,
                   position: Position | None, lots: list[TaxLot]) -> None:
    """Position rows are an aggregate cache over open lots."""
    open_lots = [l for l in lots if Decimal(l.shares_open) > 0]
    total = sum((Decimal(l.shares_open) for l in open_lots), ZERO)
    if total <= 0:
        if position is not None:
            db.delete(position)
        return
    basis = sum((Decimal(l.shares_open) * Decimal(l.cost_per_share) for l in open_lots), ZERO)
    if position is None:
        position = Position(account_id=account_id, ticker=ticker)
        db.add(position)
    position.shares = total
    position.average_cost = q_price(basis / total)


def execute_fill(db: Session, order: Order, price: Decimal, as_of: date) -> Transaction | None:
    """Fill `order` at `price` effective `as_of`. Locks account/position/lot
    rows; caller commits. Business rejections mark the order REJECTED and
    return None."""
    account = db.execute(
        for_update(select(Account).where(Account.id == order.account_id))
    ).scalar_one()
    position = db.execute(
        for_update(
            select(Position)
            .where(Position.account_id == account.id, Position.ticker == order.ticker)
        )
    ).scalar_one_or_none()
    lots = list(db.execute(
        for_update(
            select(TaxLot)
            .where(TaxLot.account_id == account.id, TaxLot.ticker == order.ticker)
            .order_by(TaxLot.acquired_on, TaxLot.created_at)
        )
    ).scalars())

    fee = q_money(Decimal(get_settings().trade_fee_usd))
    price = q_price(price)
    qty = Decimal(order.quantity)

    if order.side == OrderSide.BUY:
        if order.quantity_type == QuantityType.DOLLARS:
            gross = q_money(qty)
            shares = q_shares(gross / price)
        else:
            shares = q_shares(qty)
            gross = q_money(shares * price)
        if shares <= 0:
            return _reject_inline(order, "Order amount too small for one micro-share")
        total = gross + fee
        available = buying_power(db, account.id, exclude_order_id=order.id)
        if available < total:
            # top up from the linked external bank when permitted
            try:
                auto_fund(db, account, total - available,
                          f"External bank transfer — funding {order.ticker} purchase")
                funded = True
            except FundingError as exc:
                funded = False
                reason = str(exc)
            if not funded:
                return _reject_inline(
                    order,
                    f"Insufficient buying power: need ${total}, available "
                    f"${q_money(available)}. {reason}",
                )
        account.settlement_balance = Decimal(account.settlement_balance) - total
        lot = TaxLot(
            account_id=account.id,
            ticker=order.ticker,
            shares_open=shares,
            cost_per_share=q_price((gross + fee) / shares),
            acquired_on=as_of,
        )
        db.add(lot)
        lots.append(lot)
        realized = realized_st = realized_lt = None
    else:  # SELL — consume lots per the account's/order's cost-basis method
        if account.account_type.value == "TAXABLE":
            method = resolve_cost_basis_method(db, account, order.ticker, order.cost_basis_method)
            if method == CostBasisMethod.SPEC_ID and not order.spec_lots:
                # Specific ID with no lots named at sale time falls back to FIFO,
                # matching how brokerages handle an unspecified SpecID sale.
                method = CostBasisMethod.FIFO
        else:
            method = CostBasisMethod.FIFO  # no basis elections inside IRAs
        order.cost_basis_method = method  # record the method actually used (auditable)
        eligible = [l for l in lots if l.acquired_on <= as_of and Decimal(l.shares_open) > 0]
        available = sum((Decimal(l.shares_open) for l in eligible), ZERO)
        # shares promised to other resting sell orders are not available here
        promised = committed_shares(db, account.id, order.ticker, exclude_order_id=order.id)
        available = min(available, max(ZERO, available - promised)) if promised else available
        if order.quantity_type == QuantityType.DOLLARS:
            shares = q_shares(qty / price)
            if shares > available:
                shares = available  # sell-by-dollars caps at the sellable position
        else:
            shares = q_shares(qty)
        if shares <= 0 or shares > available:
            return _reject_inline(
                order,
                f"Insufficient shares: trying to sell {shares} {order.ticker}, "
                f"holding {available} as of {as_of}",
            )
        if method == CostBasisMethod.AVERAGE:
            asset = db.get(Asset, order.ticker)
            if asset is None or asset.asset_class != AssetClass.MUTUAL_FUND:
                return _reject_inline(
                    order,
                    "Average cost is only permitted for mutual funds (IRS rule) — "
                    "pick FIFO, HIFO, MinTax or specific lots for this security",
                )
        plan = _consumption_plan(order, method, eligible, shares, price, as_of)
        if isinstance(plan, str):
            return _reject_inline(order, plan)

        gross = q_money(shares * price)
        proceeds = gross - fee
        if proceeds <= 0:
            return _reject_inline(order, "Proceeds would not cover fees")

        avg_cost: Decimal | None = None
        if method == CostBasisMethod.AVERAGE:
            tot_sh = sum((Decimal(l.shares_open) for l in eligible), ZERO)
            tot_basis = sum((Decimal(l.shares_open) * Decimal(l.cost_per_share) for l in eligible), ZERO)
            avg_cost = tot_basis / tot_sh if tot_sh else ZERO

        st_gain = lt_gain = ZERO
        st_shares = ZERO
        for lot, take in plan:
            cost = avg_cost if avg_cost is not None else Decimal(lot.cost_per_share)
            gain = (price - cost) * take
            if _lot_term_days(lot, as_of) > LONG_TERM_DAYS:
                lt_gain += gain
            else:
                st_gain += gain
                st_shares += take
            lot.shares_open = Decimal(lot.shares_open) - take
            if Decimal(lot.shares_open) <= 0:
                db.delete(lot)
                lots.remove(lot)

        if avg_cost is not None:
            # electing average cost re-bases the remaining fund shares (IRS rule)
            for l in eligible:
                if l in lots:
                    l.cost_per_share = q_price(avg_cost)

        fee_st = q_money(fee * st_shares / shares) if shares else ZERO
        realized = q_money(st_gain + lt_gain - fee)
        realized_st = q_money(st_gain) - fee_st
        realized_lt = realized - realized_st
        account.settlement_balance = Decimal(account.settlement_balance) + proceeds

    _sync_position(db, account.id, order.ticker, position, lots)

    txn = Transaction(
        order_id=order.id,
        account_id=account.id,
        ticker=order.ticker,
        side=order.side,
        executed_price=price,
        shares_filled=shares,
        gross_amount=gross,
        fees=fee,
        realized_gains=realized,
        realized_st=realized_st,
        realized_lt=realized_lt,
        as_of=as_of,
        backdated=order.as_of is not None,
    )
    db.add(txn)
    order.status = OrderStatus.FILLED
    if order.side == OrderSide.SELL and order.exchange_to_ticker:
        _place_exchange_buy_leg(db, order, txn, as_of)
    return txn


def try_fill_limit_order(db: Session, order: Order) -> Transaction | None:
    """Fill a PENDING limit order if the market has crossed its price."""
    try:
        quote = market_data.quote(order.ticker)
    except MarketDataError:
        return None
    limit = Decimal(order.limit_price)
    crossed = quote.price <= limit if order.side == OrderSide.BUY else quote.price >= limit
    if not crossed:
        return None
    return execute_fill(db, order, quote.price, utcnow().date())


def run_due_scheduled_orders(db: Session, now: datetime | None = None) -> int:
    """Executes SCHEDULED orders whose time has come. NAV orders fill at their
    day's published close; equity orders due while the market is closed are
    pushed to the next open. Returns count processed."""
    now = now or utcnow()
    enforce = get_settings().enforce_market_hours
    from app.services.scenarios import frozen_accounts

    due = db.execute(
        for_update(
            select(Order)
            .where(Order.status == OrderStatus.SCHEDULED, Order.scheduled_for <= now,
                   Order.account_id.notin_(frozen_accounts(db))),
            skip_locked=True,
        )
    ).scalars().all()
    if due:
        from app.services.convention import ensure_fresh_for_write

        ensure_fresh_for_write()
        # These accounts are about to spend cash, so their dividend credits
        # have to be in the balance first. No due orders means no call.
        from app.services.dividends import ensure_current

        ensure_current(db, [o.account_id for o in due])
    processed = 0
    for order in due:
        asset = db.get(Asset, order.ticker)
        is_mf = asset is not None and asset.asset_class == AssetClass.MUTUAL_FUND

        if order.nav_date is not None:
            try:
                price = market_data.close_exact(order.ticker, order.nav_date)
            except MarketDataError:
                price = None
            if price is None:
                settings = get_settings()
                fill_time = cal.mf_fill_time(order.nav_date)
                if now < fill_time + timedelta(hours=settings.nav_poll_give_up_hours):
                    continue  # NAV not published yet — retry next tick
                price = _close_or_none(order.ticker, order.nav_date)
                if price is None:
                    # Our chain has failed to price this for the whole window.
                    # Before throwing away a trade the user asked for, find out
                    # which of two very different things is true: the fund has
                    # not published a NAV, or our providers cannot see one that
                    # exists. Only an independent source can tell them apart,
                    # and the answer decides whether rejecting is right.
                    from app.services.oracle import reference_close

                    confirmed = (
                        reference_close(order.ticker, order.nav_date)
                        if settings.nav_hold_max_days > 0 else None
                    )
                    hard_cap = fill_time + timedelta(days=settings.nav_hold_max_days)
                    if confirmed is not None and now < hard_cap:
                        log.error(
                            "order %s: %s NAV for %s exists (independently "
                            "confirmed at %s) but no configured provider will "
                            "return it — holding the order; check the market "
                            "data providers",
                            order.id, order.ticker, order.nav_date, confirmed)
                        continue
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = (
                        f"No NAV published for {order.ticker} on {order.nav_date} "
                        f"(confirmed against an independent source)"
                        if confirmed is None else
                        f"NAV for {order.ticker} on {order.nav_date} exists but no "
                        f"provider returned it within {settings.nav_hold_max_days} days"
                    )
                    processed += 1
                    continue
            execute_fill(db, order, price, order.nav_date)
            processed += 1
            continue

        if is_mf and enforce:
            # a user-scheduled fund order came due: route it to NAV pricing
            order.nav_date = cal.nav_date_for(now)
            order.scheduled_for = cal.mf_fill_time(order.nav_date)
            continue

        if enforce and not cal.is_market_open(now):
            order.scheduled_for = cal.next_market_open(now)
            continue

        try:
            quote = market_data.quote(order.ticker)
        except MarketDataError as exc:
            # No synthetic substitute exists, by design: filling here would
            # write an invented price into the ledger permanently. Hold the
            # order and retry — the user asked for this trade, and a provider
            # blip is not a reason to lose it or to fake it.
            give_up = timedelta(hours=get_settings().market_data_give_up_hours)
            due_at = _aware(order.scheduled_for)
            if due_at is not None and now > due_at + give_up:
                order.status = OrderStatus.REJECTED
                order.reject_reason = (
                    f"Market data unavailable for {order.ticker} for over "
                    f"{get_settings().market_data_give_up_hours}h: {exc}"
                )
                processed += 1
            else:
                log.warning("order %s held: market data unavailable (%s)", order.id, exc)
            continue
        execute_fill(db, order, _slipped(quote.price, order.side, order.id), now.date())
        processed += 1
    db.commit()
    return processed


def run_pending_limit_orders(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    if get_settings().enforce_market_hours and not cal.is_market_open(now):
        return 0
    from app.services.scenarios import frozen_accounts

    pending = db.execute(
        for_update(
            select(Order)
            .where(Order.status == OrderStatus.PENDING, Order.order_type == OrderType.LIMIT,
                   Order.account_id.notin_(frozen_accounts(db))),
            skip_locked=True,
        )
    ).scalars().all()
    filled = 0
    for order in pending:
        if order.expires_at is not None and order.expires_at <= now:
            continue  # the expiry sweep will lapse it
        if try_fill_limit_order(db, order) is not None:
            filled += 1
    db.commit()
    return filled


# ------------------------------------------------------------------ exchanges
#
# An exchange is one instruction that a brokerage executes as two legs: sell
# the source holding, then buy the destination with the net proceeds. Both
# legs live in the same account, and the buy leg is created by the sell leg's
# fill (inside the same database transaction) so the proceeds can never be
# spent by anything else in between. In a taxable account the sell leg is a
# realization event; inside an IRA it is not.

def reject_settlement_ticker(ticker: str) -> None:
    """The settlement fund is not tradable: money reaches it by deposit, sale
    proceeds or dividends, and leaves it by purchase or withdrawal."""
    from app.services import settlement

    if ticker.upper() == settlement.TICKER:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{settlement.TICKER} is your settlement fund, not a tradable holding — "
                "deposit or withdraw cash instead; purchases and sales sweep it "
                "automatically."
            ),
        )


def _exchange_price(db: Session, ticker: str, as_of: date,
                    side: OrderSide, nav_date: date | None = None) -> Decimal | None:
    """Price for a leg effective `as_of`: the historical close for a past date,
    that day's NAV for a fund printed after the close, else the live quote."""
    today = date.today()
    if as_of < today:
        return _close_or_none(ticker, as_of)
    asset = db.get(Asset, ticker)
    if nav_date is not None and asset is not None and asset.asset_class == AssetClass.MUTUAL_FUND:
        nav = _close_or_none(ticker, nav_date)
        if nav is not None:
            return nav
    try:
        price = market_data.quote(ticker).price
    except MarketDataError:
        return None
    # funds transact at NAV, so slippage applies only to exchange-traded legs
    if asset is not None and asset.asset_class == AssetClass.MUTUAL_FUND:
        return q_price(price)
    return _slipped(price, side)


def exchange_shares(db: Session, account: Account, data, price: Decimal | None) -> Decimal:
    """How many shares of the source holding the instruction sells."""
    from app.models import QuantityType as QT

    if data.spec_lots:
        return q_shares(sum((l.shares for l in data.spec_lots), ZERO))
    if data.exchange_all:
        return q_shares(max(sellable_shares(db, account.id, data.from_ticker), ZERO))
    if data.quantity is None:
        raise HTTPException(status_code=422, detail="quantity is required unless exchange_all is set")
    if data.quantity_type == QT.SHARES:
        return q_shares(Decimal(data.quantity))
    if not price or price <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"No price available for {data.from_ticker} — cannot size a dollar exchange",
        )
    return q_shares(Decimal(data.quantity) / price)


def preview_exchange(db: Session, account: Account, data):
    """What the sell leg would realize, lot by lot. Read-only: nothing is
    written, so the trade ticket can show the tax consequence before it runs."""
    from app.schemas import ExchangeLotOut, ExchangePreviewOut

    reject_settlement_ticker(data.from_ticker)
    reject_settlement_ticker(data.to_ticker)
    if data.from_ticker == data.to_ticker:
        raise HTTPException(status_code=422, detail="Pick a different fund to exchange into")
    require_asset(db, data.from_ticker)
    require_asset(db, data.to_ticker)

    if data.as_of is not None and not backdating_allowed(db, account):
        raise HTTPException(status_code=422, detail=BACKDATE_REFUSED)
    as_of = data.as_of or date.today()
    taxable = account.account_type == AccountType.TAXABLE
    price = _exchange_price(db, data.from_ticker, as_of, OrderSide.SELL)
    if price is None:
        raise HTTPException(
            status_code=422,
            detail=f"Market data unavailable for {data.from_ticker}",
        )
    shares = exchange_shares(db, account, data, price)
    if shares <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"No {data.from_ticker} shares are available to exchange in this account",
        )

    method = (
        resolve_cost_basis_method(db, account, data.from_ticker, data.cost_basis_method)
        if taxable else CostBasisMethod.FIFO
    )
    if method == CostBasisMethod.SPEC_ID and not data.spec_lots:
        method = CostBasisMethod.FIFO

    lots = list(db.execute(
        select(TaxLot)
        .where(TaxLot.account_id == account.id, TaxLot.ticker == data.from_ticker)
        .order_by(TaxLot.acquired_on, TaxLot.created_at)
    ).scalars())
    eligible = [l for l in lots if l.acquired_on <= as_of and Decimal(l.shares_open) > 0]
    held = sum((Decimal(l.shares_open) for l in eligible), ZERO)
    if shares > held:
        raise HTTPException(
            status_code=422,
            detail=f"Exchanging {shares} {data.from_ticker} but only {held} are held as of {as_of}",
        )

    stub = Order(
        account_id=account.id, ticker=data.from_ticker, side=OrderSide.SELL,
        quantity_type=QuantityType.SHARES, quantity=shares,
        spec_lots=(
            json.dumps([{"lot_id": l.lot_id, "shares": str(l.shares)} for l in data.spec_lots])
            if data.spec_lots else None
        ),
    )
    plan = _consumption_plan(stub, method, eligible, shares, price, as_of)
    if isinstance(plan, str):
        raise HTTPException(status_code=422, detail=plan)

    avg_cost: Decimal | None = None
    if method == CostBasisMethod.AVERAGE:
        tot_basis = sum((Decimal(l.shares_open) * Decimal(l.cost_per_share) for l in eligible), ZERO)
        avg_cost = tot_basis / held if held else ZERO

    fee = q_money(Decimal(get_settings().trade_fee_usd))
    gross = q_money(shares * price)
    net = gross - fee
    st = lt = basis = ZERO
    rows: list[ExchangeLotOut] = []
    for lot, take in plan:
        cost = avg_cost if avg_cost is not None else Decimal(lot.cost_per_share)
        gain = q_money((price - cost) * take)
        long_term = _lot_term_days(lot, as_of) > LONG_TERM_DAYS
        basis += q_money(cost * take)
        if long_term:
            lt += gain
        else:
            st += gain
        rows.append(ExchangeLotOut(
            acquired_on=lot.acquired_on,
            shares=take,
            cost_per_share=q_price(cost),
            proceeds=q_money(take * price),
            gain=gain,
            term="LONG" if long_term else "SHORT",
        ))

    to_price = _exchange_price(db, data.to_ticker, as_of, OrderSide.BUY)
    notes: list[str] = []
    if taxable:
        notes.append(
            "An exchange is a sale plus a purchase: the sale is reported on Form 1099-B "
            "for the year it settles, even though the money never leaves the account."
        )
        if st > 0:
            notes.append(
                f"${q_money(st)} is a short-term gain (shares held one year or less), taxed "
                "at your ordinary income rate."
            )
        elif st < 0:
            notes.append(
                f"${q_money(-st)} is a short-term loss; it offsets short-term gains first, "
                "then up to $3,000 of ordinary income per year."
            )
        if lt > 0:
            notes.append(
                f"${q_money(lt)} is a long-term gain (shares held more than one year), taxed "
                "at the 0%/15%/20% long-term capital gains rates."
            )
        elif lt < 0:
            notes.append(
                f"${q_money(-lt)} is a long-term loss; it offsets long-term gains first."
            )
        if st + lt < 0:
            notes.append(
                "This exchange realizes a loss. Buying a substantially identical fund "
                "within 30 days triggers the wash-sale rule, which defers the loss into "
                "the basis of the new shares."
            )
        notes.append(f"Cost basis method used: {method.value}.")
    else:
        notes.append(
            f"No tax impact: exchanges inside a {ACCOUNT_TYPE_WORDS[account.account_type]} "
            "are not taxable events — no gain is reported and no 1099-B is issued."
        )

    return ExchangePreviewOut(
        account_id=account.id,
        account_type=account.account_type,
        taxable=taxable,
        from_ticker=data.from_ticker,
        to_ticker=data.to_ticker,
        price=q_price(price),
        shares=shares,
        gross_proceeds=gross,
        fees=fee,
        net_proceeds=net,
        cost_basis=q_money(basis),
        cost_basis_method=method,
        short_term_gains=q_money(st),
        long_term_gains=q_money(lt),
        total_gains=q_money(st + lt - fee),
        estimated_shares_bought=q_shares(net / to_price) if to_price else None,
        lots=rows,
        notes=notes,
    )


ACCOUNT_TYPE_WORDS = {
    AccountType.TAXABLE: "taxable brokerage account",
    AccountType.ROTH_IRA: "Roth IRA",
    AccountType.TRADITIONAL_IRA: "Traditional IRA",
    AccountType.ROLLOVER_IRA: "Rollover IRA",
}


def _place_exchange_buy_leg(db: Session, sell_order: Order, txn: Transaction,
                            as_of: date) -> Order | None:
    """Reinvest a filled exchange's net proceeds in the destination symbol.
    Runs inside the sell leg's transaction, so the two legs commit together."""
    ticker = sell_order.exchange_to_ticker
    proceeds = q_money(Decimal(txn.gross_amount) - Decimal(txn.fees))
    buy = Order(
        account_id=sell_order.account_id,
        ticker=ticker,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity_type=QuantityType.DOLLARS,
        quantity=proceeds,
        as_of=sell_order.as_of,
        source=sell_order.source,
        exchange_from_order_id=sell_order.id,
        status=OrderStatus.PENDING,
    )
    db.add(buy)
    db.flush()
    if proceeds <= 0:
        _reject_inline(buy, "Exchange proceeds were zero after fees")
        return buy
    price = _exchange_price(db, ticker, as_of, OrderSide.BUY, nav_date=sell_order.nav_date)
    if price is None or price <= 0:
        _reject_inline(
            buy,
            f"Market data unavailable for {ticker}; the exchange proceeds stayed in "
            "your settlement fund",
        )
        return buy
    execute_fill(db, buy, price, as_of)
    return buy


def place_exchange(db: Session, account: Account, data, source: OrderSource):
    """Execute an exchange. Returns (sell_order, sell_txn, buy_order, buy_txn)."""
    reject_settlement_ticker(data.from_ticker)
    reject_settlement_ticker(data.to_ticker)
    if data.from_ticker == data.to_ticker:
        raise HTTPException(status_code=422, detail="Pick a different fund to exchange into")
    require_asset(db, data.from_ticker)
    require_asset(db, data.to_ticker)

    as_of = data.as_of or date.today()
    price = _exchange_price(db, data.from_ticker, as_of, OrderSide.SELL)
    if data.spec_lots or data.exchange_all or data.quantity_type == QuantityType.SHARES:
        qty_type = QuantityType.SHARES
        quantity = exchange_shares(db, account, data, price)
    else:
        qty_type = QuantityType.DOLLARS
        quantity = Decimal(data.quantity or 0)
    if quantity <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"No {data.from_ticker} shares are available to exchange in this account",
        )

    sell_in = OrderCreateIn(
        account_id=account.id,
        ticker=data.from_ticker,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity_type=qty_type,
        quantity=quantity,
        as_of=data.as_of,
        cost_basis_method=data.cost_basis_method,
        spec_lots=data.spec_lots,
    )
    sell_order, sell_txn = place_order(db, account, sell_in, source,
                                       exchange_to=data.to_ticker)
    buy_order = db.execute(
        select(Order).where(Order.exchange_from_order_id == sell_order.id)
    ).scalar_one_or_none()
    buy_txn = None
    if buy_order is not None:
        buy_txn = db.execute(
            select(Transaction).where(Transaction.order_id == buy_order.id)
        ).scalar_one_or_none()
    return sell_order, sell_txn, buy_order, buy_txn
