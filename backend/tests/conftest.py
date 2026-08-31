import os
from datetime import date
from decimal import Decimal

os.environ.setdefault("SECRET_KEY", "unit-test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("MARKET_DATA_PROVIDER", "synthetic")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:63790/9")  # unreachable: caches fail open
os.environ.setdefault("ENFORCE_MARKET_HOURS", "false")  # hours-specific tests opt back in
# The backtest engine is the subject of many tests; the gate itself is covered
# by tests that opt back out (see test_market_rules.py).
os.environ.setdefault("ALLOW_BACKDATED_TRADES", "true")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as dbmod
from app.db import Base
from app.models import Account, AccountType, Asset, AssetClass, IrsLimit, Scenario, User


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    session = dbmod._SessionLocal()
    yield session
    session.close()
    dbmod._engine = None
    dbmod._SessionLocal = None


@pytest.fixture()
def user(db):
    u = User(
        email="tester@example.com",
        password_hash="x",
        date_of_birth=date(1990, 6, 1),
    )
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def scenario(db, user):
    """Every account lives in a scenario; tests use one track unless they are
    specifically about scenarios."""
    s = Scenario(user_id=user.id, name="Scenario 1", sort_order=0)
    db.add(s)
    db.commit()
    user.default_scenario_id = s.id
    db.commit()
    return s


@pytest.fixture()
def roth(db, user, scenario):
    a = Account(user_id=user.id, scenario_id=scenario.id, account_type=AccountType.ROTH_IRA,
                name="Roth", settlement_balance=Decimal("0"))
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def voo_asset(db):
    if db.get(Asset, "VOO") is None:
        db.add(Asset(ticker="VOO", name="Vanguard S&P 500 ETF", asset_class=AssetClass.ETF,
                     expense_ratio=Decimal("0.0003")))
        db.commit()
    return db.get(Asset, "VOO")


@pytest.fixture()
def taxable(db, user, voo_asset, scenario):
    a = Account(user_id=user.id, scenario_id=scenario.id, account_type=AccountType.TAXABLE,
                name="Brokerage", settlement_balance=Decimal("10000"))
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def fund_asset(db):
    a = Asset(ticker="VFIAX", name="Vanguard 500 Index Fund Admiral",
              asset_class=AssetClass.MUTUAL_FUND, expense_ratio=Decimal("0.0004"))
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def enforce_hours(monkeypatch):
    """Opt a test into real market-hours emulation."""
    from app.config import get_settings

    s = get_settings().model_copy(update={"enforce_market_hours": True})
    monkeypatch.setattr("app.services.trading.get_settings", lambda: s)
    return s


@pytest.fixture()
def limits(db):
    for year, limit, catchup in [(2025, "7000", "1000"), (2026, "7500", "1100")]:
        db.add(IrsLimit(
            tax_year=year, ira_limit=Decimal(limit), ira_catchup=Decimal(catchup),
            designation_deadline=date(year + 1, 4, 15),
        ))
    db.commit()


class FakeRedis:
    """In-memory stand-in for the rate-limit/lockout store.

    Authentication now fails *closed* when this backend is unreachable (an
    attacker must not be able to switch off brute-force protection by knocking
    Redis over), so the auth tests need a working one rather than a refused
    connection.
    """

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, k):
        return self.store.get(k)

    def set(self, k, v, ex=None):
        self.store[k] = str(v)

    def incr(self, k):
        n = int(self.store.get(k, 0)) + 1
        self.store[k] = str(n)
        return n

    def expire(self, k, seconds):
        return True

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr("app.rate_limit.get_redis", lambda: r)
    return r
