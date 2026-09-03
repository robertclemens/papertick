import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Read a stored timestamp back as timezone-aware.

    Every DateTime column here is declared `timezone=True`, which PostgreSQL
    honours — but SQLite has no such type and hands back a naive datetime, so
    comparing a stored expiry against `utcnow()` raises rather than answering.
    Expiry checks go through this so they behave the same on both.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def SAEnum(e):  # portable across postgres/sqlite
    return Enum(e, native_enum=False, length=32, validate_strings=True)


class AccountType(str, enum.Enum):
    TAXABLE = "TAXABLE"
    ROTH_IRA = "ROTH_IRA"
    TRADITIONAL_IRA = "TRADITIONAL_IRA"
    ROLLOVER_IRA = "ROLLOVER_IRA"


IRA_TYPES = {AccountType.ROTH_IRA, AccountType.TRADITIONAL_IRA}


class CashFlowKind(str, enum.Enum):
    CONTRIBUTION = "CONTRIBUTION"
    ROLLOVER = "ROLLOVER"
    WITHDRAWAL = "WITHDRAWAL"
    # A Roth conversion, written as a signed pair: negative out of the
    # Traditional/Rollover IRA, positive into the Roth. Deliberately its own
    # kind rather than a withdrawal plus a contribution, because a conversion
    # has no annual limit and no income cap — treating it as a contribution
    # would have it consume IRA room it does not consume.
    CONVERSION = "CONVERSION"
    # The value an account was *opened* with when a scenario was copied or a
    # statement history imported: cash plus the market value of the holdings
    # carried across. Deliberately its own kind rather than a rollover, which
    # is what it used to be written as. A rollover is a specific, reportable
    # event — money leaving a retirement plan and landing in an IRA — and
    # counting an opening balance as one overstates "rollovers received" by the
    # whole value of the copied account. It is also not a contribution: it
    # consumes no annual room, because the money was already inside the
    # wrapper before the copy. It is external money in, and it is reported as
    # exactly that and nothing else.
    OPENING_BALANCE = "OPENING_BALANCE"


class AssetClass(str, enum.Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    MUTUAL_FUND = "MUTUAL_FUND"


class AssetCategory(str, enum.Enum):
    STOCK = "STOCK"
    BOND = "BOND"
    REAL_ESTATE = "REAL_ESTATE"
    MIXED = "MIXED"
    COMMODITY = "COMMODITY"  # metals, crypto trusts and other non-cash-flowing assets
    SHORT_TERM_RESERVES = "SHORT_TERM_RESERVES"  # money market / settlement fund
    OTHER = "OTHER"


class AssetRegion(str, enum.Enum):
    US = "US"
    INTERNATIONAL = "INTERNATIONAL"
    GLOBAL = "GLOBAL"
    OTHER = "OTHER"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class QuantityType(str, enum.Enum):
    SHARES = "SHARES"
    DOLLARS = "DOLLARS"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"        # limit order waiting for its price
    SCHEDULED = "SCHEDULED"    # queued for a future execution time
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"        # time-in-force elapsed before the price was met


# Statuses that still hold a claim on cash (buys) or shares (sells).
OPEN_STATUSES = (OrderStatus.PENDING, OrderStatus.SCHEDULED)


class TimeInForce(str, enum.Enum):
    DAY = "DAY"              # expires at today's close
    GTC_30 = "GTC_30"        # good till canceled, 30 days
    GTC_60 = "GTC_60"
    GTC_90 = "GTC_90"
    GTC_180 = "GTC_180"
    GTC = "GTC"              # good till canceled, 1 year (broker maximum here)


class OrderSource(str, enum.Enum):
    WEB = "WEB"
    API = "API"
    RECURRING = "RECURRING"


class Cadence(str, enum.Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUALLY = "ANNUALLY"


class RuleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class CostBasisMethod(str, enum.Enum):
    FIFO = "FIFO"          # first in, first out (IRS default)
    LIFO = "LIFO"          # last in, first out
    HIFO = "HIFO"          # highest cost first
    MIN_TAX = "MIN_TAX"    # tax-optimized: ST losses, LT losses, LT gains, ST gains
    AVERAGE = "AVERAGE"    # average cost — mutual funds only (IRS rule)
    SPEC_ID = "SPEC_ID"    # specific lot identification (lots chosen per sale)


class OptionRight(str, enum.Enum):
    CALL = "CALL"
    PUT = "PUT"


class PositionSide(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OptionAction(str, enum.Enum):
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"
    SELL_TO_OPEN = "SELL_TO_OPEN"
    BUY_TO_CLOSE = "BUY_TO_CLOSE"
    EXERCISE = "EXERCISE"
    ASSIGNMENT = "ASSIGNMENT"
    EXPIRATION = "EXPIRATION"
    CASH_SETTLEMENT = "CASH_SETTLEMENT"


class StatementKind(str, enum.Enum):
    MONTHLY = "MONTHLY"
    YEAR_END = "YEAR_END"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(60), default=None)
    last_name: Mapped[str | None] = mapped_column(String(60), default=None)
    date_of_birth: Mapped[date] = mapped_column(Date)
    mfa_secret_enc: Mapped[str | None] = mapped_column(String(512), default=None)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Opt-in passwordless: when set, this account signs in with a passkey only
    # and the password path is refused. Guarded at the point of enabling — it
    # needs two registered passkeys, so losing one authenticator is not a
    # lockout. The password hash is kept so the switch can be turned back off.
    passkey_only: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # preferred performance window (see PERFORMANCE_RANGES)
    default_range: Mapped[str] = mapped_column(String(8), default="1y")
    # scenario shown on sign-in; falls back to the first one when unset
    default_scenario_id: Mapped[str | None] = mapped_column(String(36), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="user")

    @property
    def full_name(self) -> str:
        """Display name for the signed-in user; falls back to the email local
        part so the UI always has something to show."""
        name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return name or self.email.split("@")[0]


class WebAuthnCredential(Base):
    """A registered passkey. credential_id/public_key are base64url strings."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    credential_id: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    public_key: Mapped[str] = mapped_column(String(2048))
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[str | None] = mapped_column(String(255), default=None)
    nickname: Mapped[str] = mapped_column(String(100), default="Passkey")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TrustedDevice(Base):
    """A browser this user has already signed in from successfully.

    Only consulted for accounts with no passkey and no authenticator, and only
    in production: it is the fallback second factor, not an extra one. The
    cookie carries a random secret; only its SHA-256 lands here, so a database
    read does not yield a usable device token.
    """

    __tablename__ = "trusted_devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120), default="Unknown device")
    # coarse only: enough for the user to recognise the row, never a fingerprint
    last_ip: Mapped[str | None] = mapped_column(String(45), default=None)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SecurityEventKind(str, enum.Enum):
    """Security-relevant things that happen to an account.

    Recorded whether or not an email goes out, so the trail survives a mail
    relay being down and gives the user one place to answer "was that me?".
    """

    SIGN_IN = "SIGN_IN"
    SIGN_IN_BLOCKED = "SIGN_IN_BLOCKED"
    LOCKOUT = "LOCKOUT"
    DEVICE_CODE_SENT = "DEVICE_CODE_SENT"
    DEVICE_TRUSTED = "DEVICE_TRUSTED"
    DEVICES_REVOKED = "DEVICES_REVOKED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"
    EMAIL_CHANGE_REQUESTED = "EMAIL_CHANGE_REQUESTED"
    EMAIL_CHANGED = "EMAIL_CHANGED"
    PASSKEY_ADDED = "PASSKEY_ADDED"
    PASSKEY_REMOVED = "PASSKEY_REMOVED"
    PASSWORDLESS_ENABLED = "PASSWORDLESS_ENABLED"
    PASSWORDLESS_DISABLED = "PASSWORDLESS_DISABLED"
    MFA_ENABLED = "MFA_ENABLED"
    MFA_DISABLED = "MFA_DISABLED"
    API_KEY_CREATED = "API_KEY_CREATED"
    API_KEY_REVOKED = "API_KEY_REVOKED"


class SecurityEvent(Base):
    """One entry in the account's security log.

    The originating IP is the point of the table: it is the only field that
    says *where* something came from, and it is resolved through the trusted
    proxy chain (see `rate_limit.client_ip`) rather than read straight off
    X-Forwarded-For, so a client cannot write its own history.
    """

    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[SecurityEventKind] = mapped_column(SAEnum(SecurityEventKind))
    # IPv6 needs 45 characters; "unknown" when the peer address is unavailable.
    ip: Mapped[str] = mapped_column(String(45), default="unknown")
    # Browser and platform family from the User-Agent — coarse by design.
    device: Mapped[str | None] = mapped_column(String(120), default=None)
    # What changed, in the user's terms ("old@x.com → new@y.com").
    detail: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, index=True)


class PasswordResetToken(Base):
    """A single-use, short-lived credential that authorises one password reset.

    Only the SHA-256 of the token is stored: a database read yields nothing
    usable, exactly as for refresh tokens and device tokens. `used_at` makes
    redemption single-use, and every outstanding token for the user is burned
    when any one of them is redeemed or the password changes by another route,
    so a link cannot be replayed after the fact.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Where the reset was asked for, so the confirmation email can say so.
    requested_ip: Mapped[str | None] = mapped_column(String(45), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SplitApplication(Base):
    """One split, applied once to one account's holding of one security.

    Splits are permanent facts, but applying one is destructive — it rewrites
    every open lot's share count and per-share basis — so it must happen
    exactly once. The unique key is what guarantees that: a second attempt for
    the same (account, ticker, ex-date) is refused by the database rather than
    silently doubling the position.

    The before/after share counts are kept because this is the one ledger
    mutation with no order behind it, and an unexplained change in share count
    is precisely what someone auditing the account would want traced.
    """

    __tablename__ = "split_applications"
    __table_args__ = (
        UniqueConstraint("account_id", "ticker", "event_date", name="uq_split_applied"),
        Index("ix_splits_account_ticker", "account_id", "ticker"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    event_date: Mapped[date] = mapped_column(Date)
    ratio: Mapped[object] = mapped_column(Numeric(18, 8))
    shares_before: Mapped[object] = mapped_column(Numeric(24, 6))
    shares_after: Mapped[object] = mapped_column(Numeric(24, 6))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    replaced_by: Mapped[str | None] = mapped_column(String(36), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. "ptk_Ab3d..."
    scopes: Mapped[str] = mapped_column(String(100))  # comma-separated: read,trade
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Scenario(Base):
    """An independent track of accounts, holdings and history for one user.

    Everything scenario-specific hangs off Account, so scoping a request is a
    single predicate on `Account.scenario_id`; statements are the one exception
    because they aggregate across accounts and so carry the id themselves.
    """

    __tablename__ = "scenarios"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_scenario_name"),
        Index("ix_scenarios_user_order", "user_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(String(300), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # where the holdings came from, for a scenario copied off another
    copied_from_id: Mapped[str | None] = mapped_column(String(36), default=None)
    # Whether this track accepts past-dated ("as of") fills. Off by default:
    # a backdated order is placed knowing the outcome, and it rewrites periods
    # that already have statements. It is a per-scenario choice rather than a
    # deployment-wide one because a scenario is exactly the place to run a
    # hypothesis you already know the answer to — as long as it says so.
    allow_backdated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Soft delete: the scenario and everything in it stay put for a retention
    # window so a mistaken click is recoverable, then a beat task wipes them.
    # A deleted scenario is frozen — it cannot be selected and its schedules,
    # accruals and dividends stop running.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


MAX_SCENARIOS_PER_USER = 100


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # The database is the last line of defence for money. A lost update or
        # a bad import should fail loudly here rather than settle quietly into
        # a negative or non-numeric balance that every later sum inherits.
        # `x = x` is false for NaN, so this rejects NaN and Infinity too.
        CheckConstraint("settlement_balance >= 0 AND settlement_balance = settlement_balance",
                        name="ck_account_settlement_balance_sane"),
        # Not `>= 0`: the monthly credit is rounded to the cent, so the
        # remainder carried forward is legitimately a tiny negative fraction
        # when it rounds up. Only non-finite values are wrong here.
        CheckConstraint("settlement_accrued = settlement_accrued",
                        name="ck_account_settlement_accrued_sane"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id"), index=True)
    account_type: Mapped[AccountType] = mapped_column(SAEnum(AccountType))
    name: Mapped[str] = mapped_column(String(100))
    # Cash lives in the settlement fund (VMFXX), not a bare cash balance: it is
    # swept in on deposits/sales and out on purchases, and accrues the fund's
    # daily dividend. Every dollar in it is available immediately (no holds).
    settlement_balance: Mapped[object] = mapped_column(Numeric(18, 2), default=0)
    # dividend accrued on the settlement fund since the last monthly credit
    settlement_accrued: Mapped[object] = mapped_column(Numeric(18, 8), default=0)
    settlement_accrued_through: Mapped[date | None] = mapped_column(Date, default=None)
    cost_basis_method: Mapped[CostBasisMethod] = mapped_column(
        SAEnum(CostBasisMethod), default=CostBasisMethod.FIFO
    )
    # After-tax ("basis") money in a Traditional or Rollover IRA: nondeductible
    # contributions, and after-tax dollars rolled in from an employer plan. It
    # is what makes a conversion partly tax-free, and the only reason the
    # platform can model a backdoor Roth honestly. Tracked because it is
    # elected, not inferred: whether a contribution was deductible depends on
    # income and workplace-plan coverage that this app never sees, so the user
    # declares it exactly as they would on Form 8606.
    after_tax_basis: Mapped[object] = mapped_column(Numeric(18, 2), default=0)
    # When on, a trade short of cash pulls the shortfall from the linked
    # external bank (subject to IRS contribution limits in IRAs).
    # user-chosen display order (drag and drop); ties break on created_at
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="accounts")
    positions: Mapped[list["Position"]] = relationship(back_populates="account")


class Contribution(Base):
    """Every external cash movement in/out of an account.

    tax_year is set only for IRA contributions; WITHDRAWAL rows carry a
    negative amount so plain SUMs over an account give net external flow.
    """

    __tablename__ = "contributions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    tax_year: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    amount: Mapped[object] = mapped_column(Numeric(18, 2))
    kind: Mapped[CashFlowKind] = mapped_column(SAEnum(CashFlowKind))
    memo: Mapped[str | None] = mapped_column(String(200), default=None)
    # Traditional/Rollover IRA contributions only: the user elected to make this
    # one nondeductible, so it adds to the account's after-tax basis.
    nondeductible: Mapped[bool] = mapped_column(Boolean, default=False)
    # Withdrawals only: the user attests one of the IRS exceptions to the 10%
    # early-distribution penalty applies (first home, disability, higher
    # education, substantially equal payments, and the rest). The individual
    # exceptions are not modelled — each needs facts the app cannot see — so
    # this records the claim and suppresses the penalty.
    penalty_exception: Mapped[bool] = mapped_column(Boolean, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversion(Base):
    """One Roth conversion, with the pre-tax/after-tax split it was taxed on.

    A separate table rather than a flag on Contribution because Roth withdrawal
    ordering has to walk conversions oldest-first *carrying their taxable
    split*: converted pre-tax money and converted after-tax money come out in
    that order and are penalised differently. That is three facts per
    conversion, which does not fit on a cash-flow row.

    Each conversion also starts its own five-year clock, which is why the date
    is kept here and not merely implied by the contribution timestamp.
    """

    __tablename__ = "conversions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    from_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    to_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    # the date the conversion was effective; its 5-year clock starts on Jan 1
    # of this date's year, not on the date itself
    conversion_date: Mapped[date] = mapped_column(Date, index=True)
    gross_amount: Mapped[object] = mapped_column(Numeric(18, 2))
    taxable_amount: Mapped[object] = mapped_column(Numeric(18, 2))
    nontaxable_amount: Mapped[object] = mapped_column(Numeric(18, 2))
    # how much of each part is still inside the Roth, for withdrawal ordering
    taxable_remaining: Mapped[object] = mapped_column(Numeric(18, 2))
    nontaxable_remaining: Mapped[object] = mapped_column(Numeric(18, 2))
    in_kind: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Asset(Base):
    __tablename__ = "assets"

    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass))
    expense_ratio: Mapped[object | None] = mapped_column(Numeric(6, 4), default=None)
    category: Mapped[AssetCategory] = mapped_column(SAEnum(AssetCategory), default=AssetCategory.OTHER)
    region: Mapped[AssetRegion] = mapped_column(SAEnum(AssetRegion), default=AssetRegion.OTHER)
    prospectus_url: Mapped[str | None] = mapped_column(String(400), default=None)
    auto_registered: Mapped[bool] = mapped_column(Boolean, default=False)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "ticker", name="uq_position_account_ticker"),
        CheckConstraint("shares >= 0 AND shares = shares", name="ck_position_shares_sane"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("assets.ticker"))
    shares: Mapped[object] = mapped_column(Numeric(20, 6), default=0)
    average_cost: Mapped[object] = mapped_column(Numeric(18, 6), default=0)

    account: Mapped[Account] = relationship(back_populates="positions")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_status_sched", "status", "scheduled_for"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("assets.ticker"))
    side: Mapped[OrderSide] = mapped_column(SAEnum(OrderSide))
    order_type: Mapped[OrderType] = mapped_column(SAEnum(OrderType), default=OrderType.MARKET)
    quantity_type: Mapped[QuantityType] = mapped_column(SAEnum(QuantityType))
    quantity: Mapped[object] = mapped_column(Numeric(20, 6))
    limit_price: Mapped[object | None] = mapped_column(Numeric(18, 6), default=None)
    time_in_force: Mapped[TimeInForce | None] = mapped_column(SAEnum(TimeInForce), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.PENDING)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    as_of: Mapped[date | None] = mapped_column(Date, default=None)  # historical/backtest date
    nav_date: Mapped[date | None] = mapped_column(Date, default=None)  # mutual fund: fill at this day's closing NAV
    cost_basis_method: Mapped[CostBasisMethod | None] = mapped_column(SAEnum(CostBasisMethod), default=None)
    spec_lots: Mapped[str | None] = mapped_column(Text, default=None)  # SPEC_ID: JSON [{lot_id, shares}]
    reject_reason: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[OrderSource] = mapped_column(SAEnum(OrderSource), default=OrderSource.API)
    recurring_rule_id: Mapped[str | None] = mapped_column(String(36), default=None, index=True)
    # Exchange (swap one holding for another in the same account): the SELL leg
    # carries the symbol to buy with its proceeds; the BUY leg points back at
    # the sell order it was funded by.
    exchange_to_ticker: Mapped[str | None] = mapped_column(String(12), default=None)
    exchange_from_order_id: Mapped[str | None] = mapped_column(String(36), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    side: Mapped[OrderSide] = mapped_column(SAEnum(OrderSide))
    executed_price: Mapped[object] = mapped_column(Numeric(18, 6))
    shares_filled: Mapped[object] = mapped_column(Numeric(20, 6))
    gross_amount: Mapped[object] = mapped_column(Numeric(18, 2))
    fees: Mapped[object] = mapped_column(Numeric(18, 6), default=0)
    realized_gains: Mapped[object | None] = mapped_column(Numeric(18, 2), default=None)
    realized_st: Mapped[object | None] = mapped_column(Numeric(18, 2), default=None)  # short-term portion
    realized_lt: Mapped[object | None] = mapped_column(Numeric(18, 2), default=None)  # long-term portion
    as_of: Mapped[date] = mapped_column(Date)  # effective market date of the fill
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # True when the user asked for a past date (`Order.as_of`), copied down from
    # the order so a query never has to join to find out. Deliberately NOT
    # `as_of != executed_at::date`: a mutual fund fills at a prior day's NAV as
    # a matter of forward pricing, and an expiry assignment processed late is
    # stamped at expiry — neither is someone trading on hindsight. Stored and
    # indexed because "does this book contain past-dated fills" is asked on
    # every dashboard, account and performance render.
    backdated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class RecurringRule(Base):
    __tablename__ = "recurring_rules"
    __table_args__ = (Index("ix_rules_status_next", "status", "next_run_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("assets.ticker"))
    amount: Mapped[object] = mapped_column(Numeric(18, 2))
    cadence: Mapped[Cadence] = mapped_column(SAEnum(Cadence))
    day_of_week: Mapped[int | None] = mapped_column(Integer, default=None)     # 0=Mon .. 6=Sun
    day_of_month: Mapped[int | None] = mapped_column(Integer, default=None)    # 1..28
    month_of_year: Mapped[int | None] = mapped_column(Integer, default=None)   # anchor month for QUARTERLY/ANNUALLY
    # "fund to my limit": each run is capped at the contribution room actually
    # left when it fires, so the final run of the year lands exactly on the
    # limit even if the user contributed elsewhere in the meantime
    fund_to_limit: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[RuleStatus] = mapped_column(SAEnum(RuleStatus), default=RuleStatus.ACTIVE)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaxLot(Base):
    """Open tax lots per position. Buys create lots; sells consume them FIFO
    (oldest acquired first), producing the short/long-term split on the sale."""

    __tablename__ = "tax_lots"
    __table_args__ = (Index("ix_lots_account_ticker", "account_id", "ticker", "acquired_on"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    shares_open: Mapped[object] = mapped_column(Numeric(20, 6))
    cost_per_share: Mapped[object] = mapped_column(Numeric(18, 6))  # includes allocated fees
    acquired_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dividend(Base):
    """Cash dividends credited to an account for shares held on the ex-date.
    Internal income (performance), never an external flow for TWR/IRR."""

    __tablename__ = "dividends"
    __table_args__ = (UniqueConstraint("account_id", "ticker", "event_date", name="uq_div_event"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    event_date: Mapped[date] = mapped_column(Date)  # ex-dividend date
    per_share: Mapped[object] = mapped_column(Numeric(18, 6))
    shares: Mapped[object] = mapped_column(Numeric(20, 6))
    amount: Mapped[object] = mapped_column(Numeric(18, 2))
    # True for rows loaded from a brokerage export: these are the record of
    # what was actually paid, so the ex-date reconciler leaves them alone
    # instead of second-guessing them from market data.
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IrsLimit(Base):
    __tablename__ = "irs_limits"

    tax_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    ira_limit: Mapped[object] = mapped_column(Numeric(18, 2))
    ira_catchup: Mapped[object] = mapped_column(Numeric(18, 2))
    catchup_age: Mapped[int] = mapped_column(Integer, default=50)
    # Last day contributions may still be designated to this tax year (Tax Day of the next year).
    designation_deadline: Mapped[date] = mapped_column(Date)
    # "official" = seeded from published IRS figures; "projected" = auto-carried
    # forward by the platform until official figures are entered.
    source: Mapped[str] = mapped_column(String(16), default="official")


class CostBasisOverride(Base):
    """Per-fund cost-basis election within an account (Vanguard-style)."""

    __tablename__ = "cost_basis_overrides"
    __table_args__ = (UniqueConstraint("account_id", "ticker", name="uq_cb_override"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    method: Mapped[CostBasisMethod] = mapped_column(SAEnum(CostBasisMethod))


class OptionPosition(Base):
    """Open option position. avg_premium is per share (contract = 100 shares).
    collateral records the cash reserved for short puts — it stays inside
    settlement_balance and only reduces buying power."""

    __tablename__ = "option_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "underlying", "right", "strike", "expiry", "side",
                         name="uq_option_position"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    underlying: Mapped[str] = mapped_column(String(12))
    right: Mapped[OptionRight] = mapped_column(SAEnum(OptionRight))
    strike: Mapped[object] = mapped_column(Numeric(18, 2))
    expiry: Mapped[date] = mapped_column(Date, index=True)
    side: Mapped[PositionSide] = mapped_column(SAEnum(PositionSide))
    contracts: Mapped[int] = mapped_column(Integer)
    avg_premium: Mapped[object] = mapped_column(Numeric(18, 4))
    collateral: Mapped[object] = mapped_column(Numeric(18, 2), default=0)
    opened_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OptionTransaction(Base):
    __tablename__ = "option_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    underlying: Mapped[str] = mapped_column(String(12))
    right: Mapped[OptionRight] = mapped_column(SAEnum(OptionRight))
    strike: Mapped[object] = mapped_column(Numeric(18, 2))
    expiry: Mapped[date] = mapped_column(Date)
    action: Mapped[OptionAction] = mapped_column(SAEnum(OptionAction))
    contracts: Mapped[int] = mapped_column(Integer)
    premium: Mapped[object] = mapped_column(Numeric(18, 4))     # per share
    cash_effect: Mapped[object] = mapped_column(Numeric(18, 2))  # signed: credit +, debit -
    fees: Mapped[object] = mapped_column(Numeric(18, 6), default=0)
    realized_gains: Mapped[object | None] = mapped_column(Numeric(18, 2), default=None)
    realized_st: Mapped[object | None] = mapped_column(Numeric(18, 2), default=None)
    realized_lt: Mapped[object | None] = mapped_column(Numeric(18, 2), default=None)
    underlying_price: Mapped[object | None] = mapped_column(Numeric(18, 6), default=None)
    as_of: Mapped[date] = mapped_column(Date)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Statement(Base):
    """Immutable rendered account statements, like a real brokerage archive."""

    __tablename__ = "statements"
    __table_args__ = (
        UniqueConstraint("user_id", "scenario_id", "kind", "period_start",
                         name="uq_statement_period"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # statements aggregate across accounts, so they carry the scenario directly
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id"), index=True)
    kind: Mapped[StatementKind] = mapped_column(SAEnum(StatementKind))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    pdf: Mapped[bytes] = mapped_column(LargeBinary)
