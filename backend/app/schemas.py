"""Pydantic request/response models. Strict input validation at the boundary."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import (
    AccountType,
    AssetCategory,
    AssetClass,
    AssetRegion,
    Cadence,
    CashFlowKind,
    CostBasisMethod,
    OptionAction,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    QuantityType,
    RuleStatus,
    StatementKind,
    TimeInForce,
)

# A US listing: a letter, then letters/digits/dots/hyphens, up to 12 chars.
# Symbols reach an upstream provider's URL path and a cache key, so the shape is
# pinned here rather than trusted from the caller.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")


def _ticker(value: str) -> str:
    candidate = (value or "").strip().upper()
    if not TICKER_RE.fullmatch(candidate):
        raise ValueError(
            "must be 1-12 characters: a letter followed by letters, digits, "
            "dots or hyphens"
        )
    return candidate


MAX_MONEY = Decimal("10000000")
MAX_SHARES = Decimal("1000000000")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ auth

class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512)
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    date_of_birth: date

    @field_validator("date_of_birth")
    @classmethod
    def _sane_dob(cls, v: date) -> date:
        if v.year < 1900 or v > date.today():
            raise ValueError("date_of_birth must be a past date after 1900")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512)


class MfaLoginIn(BaseModel):
    mfa_token: str = Field(max_length=2048)
    code: str = Field(min_length=6, max_length=8)


class MfaCodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class MfaSetupIn(BaseModel):
    """Starting TOTP enrolment returns the secret itself, so it re-proves the
    password rather than trusting the session cookie alone."""

    current_password: str = Field(min_length=1, max_length=512)


class MfaDisableIn(MfaCodeIn):
    password: str = Field(min_length=1, max_length=512)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class LoginOut(BaseModel):
    mfa_required: bool = False
    mfa_token: str | None = None
    verification_required: bool = False
    tokens: TokenPair | None = None


class ScenarioCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    # copies balances and holdings (priced at today's market); trades,
    # dividends and auto-invest rules are deliberately not copied
    copy_from_id: str | None = Field(default=None, max_length=36)


class ScenarioUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    is_default: bool = False


class ScenarioImportIn(BaseModel):
    payload: dict
    # empty creates a new scenario; set it to replace an existing one in place
    target_scenario_id: str | None = Field(default=None, max_length=36)
    name: str | None = Field(default=None, min_length=1, max_length=80)


class DeletedScenarioOut(BaseModel):
    """A scenario inside its retention window: still recoverable, with a clock."""

    id: str
    name: str
    description: str | None
    account_count: int
    deleted_at: datetime
    purges_at: datetime
    days_left: int
    hours_left: int
    retention_days: int


class PurgeResultOut(BaseModel):
    purged: int


class ScenarioOut(BaseModel):
    id: str
    name: str
    description: str | None
    sort_order: int
    copied_from_id: str | None
    account_count: int
    is_default: bool
    is_active: bool
    created_at: datetime


# Performance windows offered everywhere a timeframe can be picked.
PERFORMANCE_RANGES = ("1m", "3m", "6m", "1y", "3y", "5y", "10y", "all")
RangeKey = Literal["1m", "3m", "6m", "1y", "3y", "5y", "10y", "all"]


class UserOut(ORMModel):
    id: str
    email: EmailStr
    first_name: str | None = None
    last_name: str | None = None
    full_name: str = ""
    date_of_birth: date
    mfa_enabled: bool
    email_verified: bool
    default_range: RangeKey = "1y"
    default_scenario_id: str | None = None
    created_at: datetime


class EmailTokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=4096)


class ResendVerificationIn(BaseModel):
    email: EmailStr


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class ProfileUpdateIn(BaseModel):
    email: EmailStr | None = None
    first_name: str | None = Field(default=None, max_length=60)
    last_name: str | None = Field(default=None, max_length=60)
    default_range: RangeKey | None = None
    default_scenario_id: str | None = Field(default=None, max_length=36)
    current_password: str | None = Field(default=None, max_length=512)
    date_of_birth: date | None = None
    confirm_impacts: bool = False

    @field_validator("date_of_birth")
    @classmethod
    def _sane_dob(cls, v: date | None) -> date | None:
        if v is not None and (v.year < 1900 or v > date.today()):
            raise ValueError("date_of_birth must be a past date after 1900")
        return v


class DobImpactOut(BaseModel):
    warnings: list[str]


class ProfileUpdateOut(BaseModel):
    user: UserOut
    email_change: Literal["none", "applied", "verification_sent"] = "none"
    warnings: list[str] = []


class PasskeyOut(ORMModel):
    id: str
    nickname: str
    transports: str | None
    created_at: datetime
    last_used_at: datetime | None


class PasskeyRegisterStartIn(BaseModel):
    """Step-up for adding a passkey: a passkey survives a password change, so
    enrolling one re-proves the password (and TOTP, when enrolled)."""

    current_password: str = Field(min_length=1, max_length=512)
    code: str | None = Field(default=None, min_length=6, max_length=8)


class PasskeyRegisterVerifyIn(BaseModel):
    credential: dict
    nickname: str = Field(default="Passkey", max_length=100)


class PasskeyLoginVerifyIn(BaseModel):
    flow_id: str = Field(max_length=64)
    credential: dict


# ------------------------------------------------------------------ api keys

class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Literal["read", "trade"]] = Field(min_length=1)


class ApiKeyOut(ORMModel):
    id: str
    name: str
    prefix: str
    scopes: str
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedOut(BaseModel):
    api_key: ApiKeyOut
    plaintext_key: str  # shown exactly once


# ------------------------------------------------------------------ accounts

class AccountCreateIn(BaseModel):
    account_type: AccountType
    name: str = Field(min_length=1, max_length=100)


class ContributionStatusOut(BaseModel):
    """Annual IRA contribution progress for one tax year. The limit is shared
    across all of a user's IRAs, so `contributed` is the household total and
    `contributed_here` is this account's share of it.

    A prior tax year stays open for new contributions until its designation
    deadline (Tax Day), so two buckets can be live at once between January 1
    and mid-April."""

    tax_year: int
    limit: Decimal
    contributed: Decimal
    contributed_here: Decimal
    remaining: Decimal
    used_pct: float
    catchup_included: bool
    is_prior_year: bool = False
    designation_deadline: date | None = None


class AccountOut(ORMModel):
    id: str
    account_type: AccountType
    name: str
    # uninvested cash lives in the settlement fund (VMFXX), never a bare balance
    settlement_balance: Decimal
    settlement_ticker: str = "VMFXX"
    settlement_name: str = "Vanguard Federal Money Market Fund (Settlement Fund)"
    settlement_yield: Decimal | None = None      # 7-day SEC yield, annualized
    settlement_accrued: Decimal | None = None    # dividend accrued this month
    cost_basis_method: CostBasisMethod
    allow_external_funding: bool
    created_at: datetime
    # settlement balance minus short-put collateral and cash committed to open
    # buy orders; there are no settlement holds on swept-in cash
    buying_power: Decimal | None = None
    # empty for taxable accounts, which have no annual contribution limit.
    # Between Jan 1 and Tax Day this holds the prior year too, while it still
    # has room — contributions may still be designated to it.
    contribution_statuses: list[ContributionStatusOut] = []


class AccountSettingsIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    allow_external_funding: bool | None = None


class AccountOrderIn(BaseModel):
    """New display order, most-preferred first."""

    account_ids: list[str] = Field(min_length=1, max_length=12)


class DepositIn(BaseModel):
    amount: Decimal = Field(gt=0, le=MAX_MONEY, decimal_places=2)
    tax_year: int | None = Field(default=None, ge=2000, le=2100)
    kind: Literal["CONTRIBUTION", "ROLLOVER"] = "CONTRIBUTION"


class WithdrawIn(BaseModel):
    amount: Decimal = Field(gt=0, le=MAX_MONEY, decimal_places=2)


class ContributionOut(ORMModel):
    id: str
    account_id: str
    tax_year: int | None
    amount: Decimal
    kind: CashFlowKind
    memo: str | None
    timestamp: datetime


class CashFlowResultOut(BaseModel):
    account: AccountOut
    contribution: ContributionOut
    warnings: list[str] = []
    irs: "IrsStatusOut | None" = None


class IrsStatusOut(BaseModel):
    tax_year: int
    limit: Decimal
    catchup_included: bool
    contributed: Decimal
    remaining: Decimal
    source: str = "official"  # "projected" until official IRS figures are entered


# ------------------------------------------------------------------ market

class AssetOut(ORMModel):
    ticker: str
    name: str
    asset_class: AssetClass
    expense_ratio: Decimal | None
    category: AssetCategory
    region: AssetRegion
    prospectus_url: str | None
    auto_registered: bool


class MarketStatusOut(BaseModel):
    is_open: bool
    is_trading_day: bool
    next_open: datetime
    next_close: datetime
    enforce_market_hours: bool
    # off by default: past-dated fills rewrite already-issued statements
    allow_backdated_trades: bool = False
    server_time: datetime


class QuoteOut(BaseModel):
    ticker: str
    price: Decimal
    prev_close: Decimal | None
    change_pct: float | None
    as_of: datetime
    provider: str


class CandleOut(BaseModel):
    date: date
    close: Decimal


class HistoryOut(BaseModel):
    ticker: str
    provider: str
    candles: list[CandleOut]


# ------------------------------------------------------------------ orders

class SpecLotIn(BaseModel):
    lot_id: str = Field(max_length=36)
    shares: Decimal = Field(gt=0, decimal_places=6)


class OrderCreateIn(BaseModel):
    account_id: str = Field(max_length=36)
    ticker: str = Field(min_length=1, max_length=12)
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    quantity_type: QuantityType
    quantity: Decimal = Field(gt=0, decimal_places=6)
    limit_price: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY, decimal_places=6)
    time_in_force: TimeInForce | None = None  # LIMIT orders; defaults to GTC_60
    as_of: date | None = None            # historical (backtest) execution date
    scheduled_for: datetime | None = None  # future execution time
    cost_basis_method: CostBasisMethod | None = None  # sells: overrides account/fund election
    spec_lots: list[SpecLotIn] | None = Field(default=None, max_length=200)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return _ticker(v)

    @field_validator("quantity")
    @classmethod
    def _qty_bounds(cls, v: Decimal) -> Decimal:
        if v > MAX_SHARES:
            raise ValueError("quantity too large")
        return v


class OrderOut(ORMModel):
    id: str
    account_id: str
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity_type: QuantityType
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: TimeInForce | None
    expires_at: datetime | None
    status: OrderStatus
    scheduled_for: datetime | None
    as_of: date | None
    nav_date: date | None
    cost_basis_method: CostBasisMethod | None
    reject_reason: str | None
    source: str
    recurring_rule_id: str | None
    created_at: datetime


class LotOut(BaseModel):
    id: str
    account_id: str
    ticker: str
    shares_open: Decimal
    cost_per_share: Decimal
    cost_basis: Decimal
    acquired_on: date
    term: Literal["SHORT", "LONG"]
    price: Decimal | None
    unrealized_gains: Decimal | None


class CostBasisOverrideOut(BaseModel):
    ticker: str
    method: CostBasisMethod
    # AVERAGE only: True once a sale has used average cost — the averaged basis
    # of those shares is then permanent (IRS §1.1012-1(e))
    average_locked: bool = False


class CostBasisConfigOut(BaseModel):
    account_id: str
    default_method: CostBasisMethod
    overrides: list[CostBasisOverrideOut]
    notes: list[str] = []


class CostBasisUpdateIn(BaseModel):
    method: CostBasisMethod
    ticker: str | None = Field(default=None, max_length=12)  # None = account default


class TransactionOut(ORMModel):
    id: str
    order_id: str
    account_id: str
    ticker: str
    side: OrderSide
    executed_price: Decimal
    shares_filled: Decimal
    gross_amount: Decimal
    fees: Decimal
    realized_gains: Decimal | None
    realized_st: Decimal | None
    realized_lt: Decimal | None
    as_of: date
    executed_at: datetime


class DividendOut(ORMModel):
    id: str
    account_id: str
    ticker: str
    event_date: date
    per_share: Decimal
    shares: Decimal
    amount: Decimal


class OrderResultOut(BaseModel):
    order: OrderOut
    transaction: TransactionOut | None = None
    funding: str | None = None  # set when an external bank transfer covered the order


# ------------------------------------------------------------------ exchanges

class ExchangeIn(BaseModel):
    """Swap one holding for another inside a single account: sell `from_ticker`
    and immediately reinvest the proceeds in `to_ticker`."""

    account_id: str = Field(max_length=36)
    from_ticker: str = Field(min_length=1, max_length=12)
    to_ticker: str = Field(min_length=1, max_length=12)
    quantity_type: QuantityType = QuantityType.DOLLARS
    quantity: Decimal | None = Field(default=None, gt=0, decimal_places=6)
    exchange_all: bool = False          # ignore quantity, exchange the whole position
    cost_basis_method: CostBasisMethod | None = None
    spec_lots: list[SpecLotIn] | None = Field(default=None, max_length=200)
    as_of: date | None = None           # historical (backtest) exchange date

    @field_validator("from_ticker", "to_ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return _ticker(v)


class ExchangeLotOut(BaseModel):
    acquired_on: date
    shares: Decimal
    cost_per_share: Decimal
    proceeds: Decimal
    gain: Decimal
    term: Literal["SHORT", "LONG"]


class ExchangePreviewOut(BaseModel):
    """What the sell leg would realize. In an IRA nothing here is taxable, and
    `taxable` is False."""

    account_id: str
    account_type: AccountType
    taxable: bool
    from_ticker: str
    to_ticker: str
    price: Decimal
    shares: Decimal
    gross_proceeds: Decimal
    fees: Decimal
    net_proceeds: Decimal
    cost_basis: Decimal
    cost_basis_method: CostBasisMethod
    short_term_gains: Decimal
    long_term_gains: Decimal
    total_gains: Decimal
    estimated_shares_bought: Decimal | None = None
    lots: list[ExchangeLotOut] = []
    notes: list[str] = []


class ExchangeResultOut(BaseModel):
    sell: OrderResultOut
    buy: OrderResultOut | None = None
    realized_gains: Decimal | None = None
    short_term_gains: Decimal | None = None
    long_term_gains: Decimal | None = None
    taxable: bool = False
    notes: list[str] = []


# ------------------------------------------------------------------ schedules

class MaxFundingIn(BaseModel):
    """What a schedule would look like if it filled the year's remaining IRA
    contribution room evenly."""

    account_id: str = Field(max_length=36)
    cadence: Cadence
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    month_of_year: int | None = Field(default=None, ge=1, le=12)


class MaxFundingPlanOut(BaseModel):
    tax_year: int
    remaining: Decimal          # contribution room left for the year
    runs: int                   # runs this schedule has left in the year
    per_run: Decimal            # amount for every run but the last
    final_run: Decimal          # last run, trimmed so the total is exact
    total: Decimal
    first_run: datetime | None = None
    last_run: datetime | None = None
    catchup_included: bool = False
    eligible: bool = True       # False when there is nothing to plan
    notes: list[str] = []


class ScheduleCreateIn(BaseModel):
    account_id: str = Field(max_length=36)
    ticker: str = Field(min_length=1, max_length=12)
    amount: Decimal = Field(gt=0, le=MAX_MONEY, decimal_places=2)
    cadence: Cadence
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    month_of_year: int | None = Field(default=None, ge=1, le=12)
    # cap each run at the contribution room left when it fires
    fund_to_limit: bool = False

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return _ticker(v)


class ScheduleUpdateIn(BaseModel):
    """Edits apply to future runs only; executed trades are never altered."""

    ticker: str | None = Field(default=None, min_length=1, max_length=12)
    amount: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY, decimal_places=2)
    cadence: Cadence | None = None
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    month_of_year: int | None = Field(default=None, ge=1, le=12)
    fund_to_limit: bool | None = None

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return _ticker(v) if v else v


class ScheduleOut(ORMModel):
    id: str
    account_id: str
    ticker: str
    amount: Decimal
    cadence: Cadence
    day_of_week: int | None
    day_of_month: int | None
    month_of_year: int | None
    fund_to_limit: bool = False
    next_run_at: datetime
    last_run_at: datetime | None
    status: RuleStatus
    failure_count: int
    created_at: datetime


# ------------------------------------------------------------------ portfolio

class PositionOut(BaseModel):
    account_id: str
    ticker: str
    name: str
    asset_class: AssetClass
    category: AssetCategory
    region: AssetRegion
    expense_ratio: Decimal | None
    prospectus_url: str | None
    shares: Decimal
    average_cost: Decimal
    cost_basis: Decimal
    price: Decimal
    market_value: Decimal
    unrealized_gains: Decimal
    unrealized_gains_pct: float | None


class PortfolioSummaryOut(BaseModel):
    total_value: Decimal
    cash: Decimal
    reserved_cash: Decimal    # short-put collateral inside cash, not spendable
    committed_cash: Decimal   # cash backing open (pending/scheduled) buy orders
    available_to_trade: Decimal
    open_order_count: int
    invested_value: Decimal
    options_value: Decimal  # signed mark value of open option positions
    cost_basis: Decimal
    net_deposits: Decimal
    unrealized_gains: Decimal
    realized_gains: Decimal
    # A sale inside an IRA realizes a gain as bookkeeping, but there is no
    # capital-gains treatment there: no 1099-B, no tax. Split so the dashboard
    # never presents sheltered gains as if they had a tax consequence.
    realized_gains_taxable: Decimal = Decimal("0")
    realized_gains_sheltered: Decimal = Decimal("0")
    total_dividends: Decimal
    total_fees: Decimal
    accounts: list[AccountOut]


class PerformancePointOut(BaseModel):
    date: date
    value: Decimal
    net_deposits: Decimal


class PerformanceOut(BaseModel):
    """Every figure here is scoped to the requested range, so the numbers
    change with the timeframe picker (Vanguard's Performance panel)."""

    series: list[PerformancePointOut]
    twr_pct: float | None = None            # time-weighted, over the period
    irr_pct: float | None = None            # money-weighted, annualized
    rate_of_return_pct: float | None = None  # headline "Rate of return"
    rate_of_return_annualized: bool = False  # False -> period return, not annualized
    beginning_balance: Decimal = Decimal("0")
    ending_balance: Decimal = Decimal("0")
    net_cash_flow: Decimal = Decimal("0")    # deposits & withdrawals in period
    investment_returns: Decimal = Decimal("0")  # ending - beginning - net flow
    dividends: Decimal = Decimal("0")
    period_start: date | None = None
    period_end: date | None = None


class AccountReturnOut(BaseModel):
    """One row of the account list: balance plus period performance."""

    account_id: str
    name: str
    account_type: AccountType
    balance: Decimal
    settlement_balance: Decimal
    investment_returns: Decimal
    rate_of_return_pct: float | None
    rate_of_return_annualized: bool


class SettlementOut(BaseModel):
    """The settlement fund position behind an account's uninvested cash."""

    account_id: str
    account_name: str
    ticker: str
    name: str
    balance: Decimal
    shares: Decimal
    nav: Decimal
    accrued_dividend: Decimal
    seven_day_yield: Decimal


class AccountReturnsOut(BaseModel):
    range: str
    period_start: date | None
    period_end: date | None
    accounts: list[AccountReturnOut]
    total_balance: Decimal
    total_investment_returns: Decimal
    total_rate_of_return_pct: float | None


# ------------------------------------------------------------------ options

class OptionQuoteOut(BaseModel):
    bid: Decimal
    ask: Decimal
    mid: Decimal
    iv: float
    delta: float
    theta: float
    itm: bool


class ChainRowOut(BaseModel):
    strike: Decimal
    call: OptionQuoteOut
    put: OptionQuoteOut


class ChainOut(BaseModel):
    underlying: str
    spot: Decimal
    expiry: date
    days_to_expiry: int
    rows: list[ChainRowOut]
    pricing_model: str = "black-scholes (model-derived quotes)"


class OptionOrderIn(BaseModel):
    account_id: str = Field(max_length=36)
    underlying: str = Field(min_length=1, max_length=12)
    right: OptionRight
    strike: Decimal = Field(gt=0, le=MAX_MONEY, decimal_places=2)
    expiry: date
    action: Literal["BUY_TO_OPEN", "SELL_TO_OPEN"]
    contracts: int = Field(ge=1, le=1000)

    @field_validator("underlying")
    @classmethod
    def _upper(cls, v: str) -> str:
        return _ticker(v)


class OptionCloseIn(BaseModel):
    contracts: int = Field(ge=1, le=1000)


class OptionPositionOut(ORMModel):
    id: str
    account_id: str
    underlying: str
    right: OptionRight
    strike: Decimal
    expiry: date
    side: PositionSide
    contracts: int
    avg_premium: Decimal
    collateral: Decimal
    opened_on: date


class OptionPositionViewOut(BaseModel):
    position: OptionPositionOut
    mark: Decimal
    market_value: Decimal      # signed: short positions are a liability
    unrealized_gains: Decimal
    underlying_price: Decimal
    days_to_expiry: int
    itm: bool


class OptionTransactionOut(ORMModel):
    id: str
    account_id: str
    underlying: str
    right: OptionRight
    strike: Decimal
    expiry: date
    action: OptionAction
    contracts: int
    premium: Decimal
    cash_effect: Decimal
    fees: Decimal
    realized_gains: Decimal | None
    realized_st: Decimal | None
    realized_lt: Decimal | None
    underlying_price: Decimal | None
    as_of: date
    executed_at: datetime


class OptionOrderResultOut(BaseModel):
    position: OptionPositionOut | None
    transaction: OptionTransactionOut
    explanation: str


# ------------------------------------------------------------------ statements

class StatementOut(ORMModel):
    id: str
    kind: StatementKind
    period_start: date
    period_end: date
    generated_at: datetime


# ------------------------------------------------------------------ taxes

class TaxYearSummaryOut(BaseModel):
    year: int
    account_id: str | None
    # taxable-brokerage activity
    short_term_gains: Decimal
    long_term_gains: Decimal
    unclassified_gains: Decimal  # sales recorded before lot tracking existed
    dividends: Decimal
    fees: Decimal
    # retirement activity
    traditional_withdrawals: Decimal  # ordinary income if this were real
    roth_withdrawals: Decimal
    ira_contributions: Decimal        # designated to this tax year
    rollovers: Decimal
    notes: list[str]


CashFlowResultOut.model_rebuild()
