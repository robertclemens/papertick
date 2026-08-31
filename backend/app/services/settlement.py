"""Settlement fund (VMFXX).

Accounts do not hold a bare cash balance: uninvested money sits in the
settlement fund, a federal money market fund modeled on Vanguard's VMFXX
(stable $1.00 NAV). Deposits and sale proceeds sweep in, purchases and
withdrawals sweep out, and the balance earns the fund's dividend, accrued
daily and credited on the last day of each month like the real fund.

Every dollar in the settlement fund is available immediately — this platform
models no settlement/clearing holds on swept-in cash. (Cash still gets
earmarked while an *open buy order* or a short-put collateral obligation is
outstanding; that is an obligation of the account, not a hold on settlement.)

Accrual runs from a per-account date cursor (`settlement_accrued_through`), so
the daily beat task is idempotent and a missed day is picked up on the next
run. Each day accrues on the balance the account holds when the task runs,
which is exactly how a daily-accruing money market fund behaves as long as the
task runs daily.
"""

import logging
from calendar import monthrange
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, Asset, AssetCategory, AssetClass, AssetRegion, Dividend

log = logging.getLogger("papertick.settlement")

TICKER = "VMFXX"
NAME = "Vanguard Federal Money Market Fund (Settlement Fund)"
NAV = Decimal("1.00")
EXPENSE_RATIO = Decimal("0.0011")
CENT = Decimal("0.01")
ZERO = Decimal("0")
DAY_COUNT = Decimal("365")

# VMFXX 7-day SEC yield, by the date each level took effect. Approximates the
# fund's published history so accruals track real money-market rates; the
# current level can be overridden with SETTLEMENT_YIELD_ANNUAL.
YIELD_HISTORY: list[tuple[date, Decimal]] = [
    (date(2015, 1, 1), Decimal("0.0001")),
    (date(2016, 1, 1), Decimal("0.0025")),
    (date(2017, 1, 1), Decimal("0.0070")),
    (date(2018, 1, 1), Decimal("0.0130")),
    (date(2019, 1, 1), Decimal("0.0230")),
    (date(2020, 4, 1), Decimal("0.0020")),
    (date(2021, 1, 1), Decimal("0.0001")),
    (date(2022, 4, 1), Decimal("0.0020")),
    (date(2022, 7, 1), Decimal("0.0150")),
    (date(2022, 10, 1), Decimal("0.0290")),
    (date(2023, 1, 1), Decimal("0.0420")),
    (date(2023, 8, 1), Decimal("0.0528")),
    (date(2024, 10, 1), Decimal("0.0480")),
    (date(2025, 1, 1), Decimal("0.0430")),
    (date(2025, 10, 1), Decimal("0.0405")),
    (date(2026, 1, 1), Decimal("0.0395")),
]


def q_money(v: Decimal) -> Decimal:
    return v.quantize(CENT, ROUND_HALF_UP)


def yield_on(day: date) -> Decimal:
    """The fund's 7-day SEC yield in effect on `day` (annualized, as a rate)."""
    override = Decimal(str(get_settings().settlement_yield_annual or 0))
    if override > 0 and day >= YIELD_HISTORY[-1][0]:
        return override
    rate = YIELD_HISTORY[0][1]
    for effective, value in YIELD_HISTORY:
        if effective <= day:
            rate = value
        else:
            break
    return rate


def current_yield() -> Decimal:
    return yield_on(date.today())


def ensure_asset(db: Session) -> Asset:
    """The settlement fund is a real (non-tradable) asset row so dividends,
    statements and holdings views can join against it."""
    asset = db.get(Asset, TICKER)
    if asset is None:
        asset = Asset(ticker=TICKER, name=NAME, asset_class=AssetClass.MUTUAL_FUND)
        db.add(asset)
    asset.name = NAME
    asset.asset_class = AssetClass.MUTUAL_FUND
    asset.expense_ratio = EXPENSE_RATIO
    asset.category = AssetCategory.SHORT_TERM_RESERVES
    asset.region = AssetRegion.US
    asset.auto_registered = False
    return asset


def _month_end(day: date) -> bool:
    return day.day == monthrange(day.year, day.month)[1]


def _credit(db: Session, account: Account, day: date) -> Decimal:
    """Pay out the accrued dividend as a monthly distribution. Whole cents are
    credited; the sub-cent remainder rides along to the next month."""
    accrued = Decimal(account.settlement_accrued or 0)
    amount = q_money(accrued.quantize(Decimal("0.00000001")))
    if amount <= 0:
        return ZERO
    existing = db.execute(
        select(Dividend).where(
            Dividend.account_id == account.id,
            Dividend.ticker == TICKER,
            Dividend.event_date == day,
        )
    ).scalar_one_or_none()
    if existing is not None:  # already credited for this month
        account.settlement_accrued = ZERO
        return ZERO
    balance = Decimal(account.settlement_balance)
    shares = balance if balance > 0 else amount  # NAV is $1.00, so shares == dollars
    db.add(Dividend(
        account_id=account.id,
        ticker=TICKER,
        event_date=day,
        per_share=(amount / shares).quantize(Decimal("0.000001")) if shares else ZERO,
        shares=shares,
        amount=amount,
    ))
    account.settlement_balance = balance + amount
    account.settlement_accrued = accrued - amount
    log.info("settlement dividend %s: $%s on %s", account.id, amount, day)
    return amount


def accrue_account(db: Session, account: Account, through: date | None = None) -> Decimal:
    """Accrue (and, at month end, credit) the settlement fund dividend up to
    `through`. Returns the dividend credited. Caller commits."""
    through = through or date.today()
    cursor = account.settlement_accrued_through
    if cursor is None:  # first run for this account: start the clock, accrue nothing
        account.settlement_accrued_through = through
        return ZERO
    if cursor >= through:
        return ZERO

    credited = ZERO
    day = cursor + timedelta(days=1)
    while day <= through:
        balance = Decimal(account.settlement_balance)
        if balance > 0:
            account.settlement_accrued = (
                Decimal(account.settlement_accrued or 0)
                + balance * yield_on(day) / DAY_COUNT
            )
        if _month_end(day):
            credited += _credit(db, account, day)
        day += timedelta(days=1)
    account.settlement_accrued_through = through
    return credited


def accrue_all(db: Session, through: date | None = None) -> Decimal:
    """Daily beat entry point: accrue every account outside a deleted scenario
    — a scenario awaiting purge must not keep earning. Caller commits."""
    from app.services.scenarios import frozen_accounts

    total = ZERO
    for account in db.execute(
        select(Account).where(Account.id.notin_(frozen_accounts(db)))
    ).scalars():
        total += accrue_account(db, account, through)
    return total


def holding_view(account: Account) -> dict:
    """The settlement fund rendered as a holding, the way a brokerage shows it
    alongside the account's investments."""
    balance = Decimal(account.settlement_balance)
    return {
        "ticker": TICKER,
        "name": NAME,
        "balance": q_money(balance),
        "shares": q_money(balance),   # $1.00 NAV
        "nav": NAV,
        "accrued_dividend": q_money(Decimal(account.settlement_accrued or 0)),
        "seven_day_yield": current_yield(),
    }
