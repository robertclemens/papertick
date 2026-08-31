"""Startup initialization: wait for the DB, migrate schema, seed reference data.

Schema evolution is handled by create_all (new tables) plus lightweight,
idempotent migrations for tables that already exist: column renames, new enum
values, then a column-adder. Run as:
python -m app.init_db
"""

import enum
import logging
import sys
import time
from datetime import date
from decimal import Decimal

from sqlalchemy import inspect, select, text

from app.config import get_settings
from app.db import Base, get_engine, get_sessionmaker
from app import models  # noqa: F401  (register all tables)
from app.models import (
    Asset,
    AssetCategory as C,
    AssetClass as K,
    AssetRegion as R,
    IrsLimit,
    OrderSide,
    Position,
    TaxLot,
    Transaction,
    User,
)
from app import security
from app.services import settlement

log = logging.getLogger("papertick.init")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _edgar(ticker: str) -> str:
    return f"https://www.sec.gov/edgar/search/#/q=%22{ticker}%22&forms=485BPOS"


# (ticker, name, class, expense_ratio, category, region, prospectus_url)
ASSET_UNIVERSE = [
    ("VOO", "Vanguard S&P 500 ETF", K.ETF, "0.0003", C.STOCK, R.US, _edgar("VOO")),
    ("VTI", "Vanguard Total Stock Market ETF", K.ETF, "0.0003", C.STOCK, R.US, _edgar("VTI")),
    ("SPY", "SPDR S&P 500 ETF Trust", K.ETF, "0.0009", C.STOCK, R.US, _edgar("SPY")),
    ("QQQ", "Invesco QQQ Trust", K.ETF, "0.0020", C.STOCK, R.US, _edgar("QQQ")),
    ("SCHD", "Schwab U.S. Dividend Equity ETF", K.ETF, "0.0006", C.STOCK, R.US, _edgar("SCHD")),
    ("VXUS", "Vanguard Total International Stock ETF", K.ETF, "0.0008", C.STOCK, R.INTERNATIONAL, _edgar("VXUS")),
    ("VNQ", "Vanguard Real Estate ETF", K.ETF, "0.0013", C.REAL_ESTATE, R.US, _edgar("VNQ")),
    ("BND", "Vanguard Total Bond Market ETF", K.ETF, "0.0003", C.BOND, R.US, _edgar("BND")),
    ("AGG", "iShares Core U.S. Aggregate Bond ETF", K.ETF, "0.0003", C.BOND, R.US, _edgar("AGG")),
    ("VIG", "Vanguard Dividend Appreciation ETF", K.ETF, "0.0006", C.STOCK, R.US, _edgar("VIG")),
    ("AAPL", "Apple Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("MSFT", "Microsoft Corporation", K.EQUITY, None, C.STOCK, R.US, None),
    ("GOOGL", "Alphabet Inc. Class A", K.EQUITY, None, C.STOCK, R.US, None),
    ("AMZN", "Amazon.com Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("NVDA", "NVIDIA Corporation", K.EQUITY, None, C.STOCK, R.US, None),
    ("META", "Meta Platforms Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("TSLA", "Tesla Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("BRK-B", "Berkshire Hathaway Inc. Class B", K.EQUITY, None, C.STOCK, R.US, None),
    ("JPM", "JPMorgan Chase & Co.", K.EQUITY, None, C.STOCK, R.US, None),
    ("V", "Visa Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("JNJ", "Johnson & Johnson", K.EQUITY, None, C.STOCK, R.US, None),
    ("UNH", "UnitedHealth Group Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("HD", "The Home Depot Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("KO", "The Coca-Cola Company", K.EQUITY, None, C.STOCK, R.US, None),
    ("PEP", "PepsiCo Inc.", K.EQUITY, None, C.STOCK, R.US, None),
    ("DIS", "The Walt Disney Company", K.EQUITY, None, C.STOCK, R.US, None),
    ("VFIAX", "Vanguard 500 Index Fund Admiral", K.MUTUAL_FUND, "0.0004", C.STOCK, R.US, _edgar("VFIAX")),
    ("VTSAX", "Vanguard Total Stock Market Index Admiral", K.MUTUAL_FUND, "0.0004", C.STOCK, R.US, _edgar("VTSAX")),
    ("FXAIX", "Fidelity 500 Index Fund", K.MUTUAL_FUND, "0.000015", C.STOCK, R.US, _edgar("FXAIX")),
    ("SWPPX", "Schwab S&P 500 Index Fund", K.MUTUAL_FUND, "0.0002", C.STOCK, R.US, _edgar("SWPPX")),
]

# (tax_year, ira_limit, catch_up, designation deadline = next year's Tax Day)
IRS_LIMITS = [
    (2024, "7000", "1000", date(2025, 4, 15)),
    (2025, "7000", "1000", date(2026, 4, 15)),
    (2026, "7500", "1100", date(2027, 4, 15)),
]


# Renames applied before anything else, so existing data follows the column
# rather than being stranded next to a freshly added empty one.
# (table, old column, new column)
COLUMN_RENAMES = [
    ("accounts", "cash_balance", "settlement_balance"),
    ("transactions", "realized_pnl", "realized_gains"),
    ("option_transactions", "realized_pnl", "realized_gains"),
]


def wait_for_db(timeout_seconds: int = 90) -> None:
    engine = get_engine()
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                log.error("database not reachable: %s", exc)
                sys.exit(1)
            time.sleep(1.5)


def apply_renames(engine) -> None:
    """Rename columns whose model attribute changed (idempotent)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, old, new in COLUMN_RENAMES:
            if not insp.has_table(table):
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if old in cols and new not in cols:
                conn.execute(text(f'ALTER TABLE {table} RENAME COLUMN "{old}" TO "{new}"'))
                log.info("schema: renamed %s.%s -> %s", table, old, new)


def ensure_enum_values(engine) -> None:
    """Add enum labels the models declare but the database type lacks
    (PostgreSQL only; other backends store enums as VARCHAR)."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                enum_name = getattr(col.type, "name", None)
                labels = getattr(col.type, "enums", None)
                if not enum_name or not labels:
                    continue
                exists = conn.execute(
                    text("SELECT 1 FROM pg_type WHERE typname = :n"), {"n": enum_name}
                ).first()
                if not exists:
                    continue
                for label in labels:
                    if not label.replace("_", "").isalnum():
                        continue  # DDL takes a literal, so only accept safe labels
                    conn.execute(
                        text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{label}'")
                    )


def _sql_default(col) -> str | None:
    default = getattr(col.default, "arg", None)
    if isinstance(default, enum.Enum):
        return f"'{default.value}'"
    if isinstance(default, bool):
        return "true" if default else "false"
    if isinstance(default, (int, Decimal)):
        return str(default)
    if isinstance(default, str):
        return f"'{default}'"
    return None


def ensure_schema(engine) -> None:
    """Add columns the models define but existing tables lack (idempotent)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                coltype = col.type.compile(engine.dialect)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype}'
                if not col.nullable:
                    default = _sql_default(col)
                    if default is None:
                        log.warning("cannot add NOT NULL column %s.%s without default; adding nullable",
                                    table.name, col.name)
                    else:
                        ddl += f" DEFAULT {default} NOT NULL"
                conn.execute(text(ddl))
                log.info("schema: added %s.%s", table.name, col.name)


def backfill_scenarios(db) -> None:
    """Everything that existed before scenarios belongs to the user's first one.

    Creates "Scenario 1" for any user whose accounts or statements are not yet
    assigned, points those rows at it, and makes it their default view."""
    from app.models import Account, Scenario, Statement, User

    users = db.execute(select(User)).scalars().all()
    for user in users:
        orphan_accounts = db.execute(
            select(Account).where(Account.user_id == user.id,
                                  Account.scenario_id.is_(None))
        ).scalars().all()
        orphan_statements = db.execute(
            select(Statement).where(Statement.user_id == user.id,
                                    Statement.scenario_id.is_(None))
        ).scalars().all()
        existing = db.execute(
            select(Scenario).where(Scenario.user_id == user.id)
            .order_by(Scenario.sort_order, Scenario.created_at)
        ).scalars().first()
        if not orphan_accounts and not orphan_statements and existing is not None:
            continue
        scenario = existing
        if scenario is None:
            scenario = Scenario(user_id=user.id, name="Scenario 1", sort_order=0,
                                description="Your original accounts and history")
            db.add(scenario)
            db.flush()
            log.info("created %r for %s", scenario.name, user.email)
        for row in orphan_accounts:
            row.scenario_id = scenario.id
        for row in orphan_statements:
            row.scenario_id = scenario.id
        if user.default_scenario_id is None:
            user.default_scenario_id = scenario.id
    db.flush()


def backfill_tax_lots(db) -> None:
    """Positions created before lot tracking get one synthetic lot at their
    average cost, acquired on their earliest buy date."""
    positions = db.execute(select(Position)).scalars().all()
    for pos in positions:
        has_lot = db.execute(
            select(TaxLot.id)
            .where(TaxLot.account_id == pos.account_id, TaxLot.ticker == pos.ticker)
            .limit(1)
        ).first()
        if has_lot:
            continue
        earliest = db.execute(
            select(Transaction.as_of)
            .where(
                Transaction.account_id == pos.account_id,
                Transaction.ticker == pos.ticker,
                Transaction.side == OrderSide.BUY,
            )
            .order_by(Transaction.as_of)
            .limit(1)
        ).scalar_one_or_none()
        db.add(TaxLot(
            account_id=pos.account_id,
            ticker=pos.ticker,
            shares_open=pos.shares,
            cost_per_share=pos.average_cost,
            acquired_on=earliest or date.today(),
        ))
        log.info("backfilled tax lot for %s/%s", pos.account_id, pos.ticker)


def seed() -> None:
    db = get_sessionmaker()()
    try:
        for ticker, name, klass, er, category, region, prospectus in ASSET_UNIVERSE:
            asset = db.get(Asset, ticker)
            if asset is None:
                asset = Asset(ticker=ticker, name=name, asset_class=klass)
                db.add(asset)
            # keep curated metadata in sync on every start
            asset.name = name
            asset.asset_class = klass
            asset.expense_ratio = Decimal(er) if er else None
            asset.category = category
            asset.region = region
            asset.prospectus_url = prospectus
            asset.auto_registered = False
        for year, limit, catchup, deadline in IRS_LIMITS:
            row = db.get(IrsLimit, year)
            if row is None:
                db.add(IrsLimit(
                    tax_year=year, ira_limit=Decimal(limit),
                    ira_catchup=Decimal(catchup), designation_deadline=deadline,
                    source="official",
                ))
            elif row.source == "projected":
                # official figures replace an auto-projected year
                row.ira_limit = Decimal(limit)
                row.ira_catchup = Decimal(catchup)
                row.designation_deadline = deadline
                row.source = "official"
        settlement.ensure_asset(db)
        backfill_scenarios(db)
        backfill_tax_lots(db)
        db.commit()

        from app.services.irs import ensure_limits
        ensure_limits(db)

        s = get_settings()
        if s.demo_mode and s.demo_email and s.demo_password:
            existing = db.execute(select(User).where(User.email == s.demo_email.lower())).first()
            if existing is None:
                security.validate_password_strength(s.demo_password)
                db.add(User(
                    email=s.demo_email.lower(),
                    password_hash=security.hash_password(s.demo_password),
                    date_of_birth=date(1990, 1, 15),
                ))
                db.commit()
                log.info("seeded demo user %s", s.demo_email)
    finally:
        db.close()


def main() -> None:
    wait_for_db()
    engine = get_engine()
    apply_renames(engine)
    ensure_enum_values(engine)
    ensure_schema(engine)
    Base.metadata.create_all(engine)
    seed()
    log.info("database ready")


if __name__ == "__main__":
    main()
