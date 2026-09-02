"""Nothing reaches a market-data provider unless something needs it.

The providers are rate-limited by their operators and Yahoo never agreed to
serve us at all, so an idle deployment — no users, no due orders, markets shut
— must be silent. These count real provider calls rather than trusting the
call graph.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import OrderSide, OrderSource
from app.schemas import OrderCreateIn
from app.services import dividends as div
from app.services import trading
from app.services.market_data import market_data


@pytest.fixture()
def fake_redis(monkeypatch):
    """The suite runs without Redis, where the fingerprint store fails open and
    every holding is re-fetched. That fallback is deliberate — a Redis blip
    must cost a redundant request, never a wrong balance — but it hides the
    skip, so these tests supply a store."""
    store: dict[str, str] = {}

    class _Fake:
        def get(self, k):
            return store.get(k)

        def set(self, k, v, ex=None):
            store[k] = v

    monkeypatch.setattr(div, "get_redis", lambda: _Fake())
    return store


@pytest.fixture()
def counter(monkeypatch):
    """Counts every call that would leave the process."""
    calls = {"quote": 0, "history": 0, "dividends": 0}

    for name in calls:
        original = getattr(market_data, name)

        def wrapped(*a, _n=name, _o=original, **kw):
            calls[_n] += 1
            return _o(*a, **kw)

        monkeypatch.setattr(market_data, name, wrapped)
    return calls


def _buy(db, account, ticker="VOO", dollars="5000", days_ago=None):
    return trading.place_order(
        db, account,
        OrderCreateIn(account_id=account.id, ticker=ticker, side="BUY",
                      quantity_type="DOLLARS", quantity=Decimal(dollars),
                      as_of=(date.today() - timedelta(days=days_ago)) if days_ago else None),
        OrderSource.API,
    )


def test_idle_workers_touch_no_provider(db, user, taxable, scenario, counter):
    """The 60-second tasks, with nothing due, must be completely silent."""
    from app.services.options import process_expirations
    from app.services.settlement import accrue_all

    assert trading.run_due_scheduled_orders(db) == 0
    assert trading.run_pending_limit_orders(db) == 0
    assert trading.expire_due_orders(db) == 0
    assert process_expirations(db) == 0
    accrue_all(db)

    assert counter == {"quote": 0, "history": 0, "dividends": 0}


def test_reconcile_fetches_once_per_ticker_not_once_per_holding(
    db, user, taxable, roth, scenario, counter
):
    """Two accounts holding the same fund is one calendar lookup, not two."""
    _buy(db, taxable, "VOO", days_ago=400)
    _buy(db, roth, "VOO", days_ago=300)
    db.commit()
    counter["dividends"] = 0

    div.reconcile_all(db)
    db.commit()
    assert counter["dividends"] == 1, "one lookup for the one distinct security"


def test_a_closed_holding_stops_being_asked_about(db, user, taxable, scenario, counter, fake_redis):
    """A fund you have fully exited cannot accrue anything new, so the sweep
    must stop fetching its calendar — that is the whole idle-cost problem."""
    _buy(db, taxable, "VOO", days_ago=400)
    db.commit()

    div.reconcile_all(db)   # first pass: fetches, and records the fingerprint
    db.commit()
    counter["dividends"] = 0

    # sell out completely
    from app.models import Position
    held = db.query(Position).filter(Position.account_id == taxable.id,
                                     Position.ticker == "VOO").one()
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side="SELL",
                      quantity_type="SHARES", quantity=Decimal(held.shares)),
        OrderSource.API,
    )
    db.commit()

    div.reconcile_all(db)   # still fetches once: history changed
    db.commit()
    assert counter["dividends"] == 1
    counter["dividends"] = 0

    for _ in range(5):      # now settled — every later sweep is free
        div.reconcile_all(db)
        db.commit()
    assert counter["dividends"] == 0, "a closed, unchanged holding must not be re-fetched"


def test_an_open_holding_is_still_checked_every_sweep(db, user, taxable, scenario, counter, fake_redis):
    """The skip must never apply to a position that can still go ex-dividend."""
    _buy(db, taxable, "VOO", days_ago=400)
    db.commit()

    div.reconcile_all(db)
    db.commit()
    counter["dividends"] = 0

    div.reconcile_all(db)
    db.commit()
    assert counter["dividends"] == 1, "an open position must keep being checked"


# ------------------------------------------------- synthetic is opt-in only

def test_auto_mode_never_falls_back_to_synthetic(monkeypatch):
    """Fabricated prices must not be reachable without asking for them.

    A fill is permanent: an order executed against an invented quote during a
    provider outage sits in the ledger forever, indistinguishable from a real
    one, corrupting cost basis, returns and tax lots. `auto` must therefore
    contain only real providers.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "market_data_provider", "auto", raising=False)
    names = [p.name for p in market_data._chain()]
    assert "synthetic" not in names, f"synthetic reachable in auto mode: {names}"
    assert names, "auto must still offer a real provider"


def test_synthetic_is_reachable_when_asked_for(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "market_data_provider", "synthetic", raising=False)
    assert [p.name for p in market_data._chain()] == ["synthetic"]


def test_outage_serves_the_last_real_price_not_an_invented_one(db, monkeypatch):
    """With every provider down, a caller gets stale-but-genuine, or an error."""
    from app.config import get_settings
    from app.services.market_data import MarketDataError

    monkeypatch.setattr(get_settings(), "market_data_provider", "auto", raising=False)

    def dead(*a, **kw):
        raise MarketDataError("provider unreachable")

    monkeypatch.setattr(market_data, "_try_chain", dead)
    monkeypatch.setattr(market_data, "_cache_get", lambda k: None)
    with pytest.raises(MarketDataError):
        market_data.quote("VOO")


def test_a_provider_outage_holds_a_due_order_rather_than_faking_a_fill(
    db, user, taxable, scenario, monkeypatch
):
    """The order the user asked for is neither lost nor filled at a made-up
    price — it waits for real data."""
    from app.models import Order, OrderStatus, OrderType, QuantityType
    from app.services.market_data import MarketDataError
    from app.services import market_calendar as cal
    from app.models import utcnow

    order = Order(
        account_id=taxable.id, ticker="VOO", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity_type=QuantityType.DOLLARS,
        quantity=Decimal("500"), status=OrderStatus.SCHEDULED,
        scheduled_for=utcnow() - timedelta(minutes=5), source=OrderSource.API,
    )
    db.add(order)
    db.commit()

    def dead(*a, **kw):
        raise MarketDataError("provider unreachable")

    monkeypatch.setattr(market_data, "quote", dead)
    trading.run_due_scheduled_orders(db)
    db.refresh(order)
    assert order.status == OrderStatus.SCHEDULED, "a data outage must not reject the order"

    # and no transaction was invented for it
    from app.models import Transaction
    assert db.query(Transaction).filter(Transaction.order_id == order.id).count() == 0


# ------------------------------------------- dividends are demand-driven now

def test_no_scheduled_task_reconciles_dividends_on_a_clock():
    """The sweep must not be on beat: an idle deployment has no reason to ask
    an upstream provider anything."""
    from app.workers.celery_app import celery

    tasks = {e["task"] for e in celery.conf.beat_schedule.values()}
    assert "app.workers.tasks.reconcile_dividends" not in tasks


def test_ensure_current_is_once_per_account_per_day(
    db, user, taxable, scenario, counter, fake_redis
):
    _buy(db, taxable, "VOO", days_ago=400)
    db.commit()
    counter["dividends"] = 0

    div.ensure_current(db, [taxable.id])
    db.commit()
    first = counter["dividends"]
    assert first >= 1

    for _ in range(5):
        div.ensure_current(db, [taxable.id])
        db.commit()
    assert counter["dividends"] == first, "repeat reads in the same day are free"


# ------------------------------------------------ market-aware refresh cadence

def test_refresh_cadence_is_zero_while_the_market_is_shut():
    """The UI must not re-price when nothing it could fetch has changed.

    The NYSE is closed for 81% of the week; polling through that is exactly the
    request whose answer is knowably identical to the last one."""
    from datetime import datetime, timezone

    from app.services import market_calendar as cal

    cases = {
        # Tue 2026-09-01, 14:00 ET — mid-session
        datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc): ("open", 60),
        # Tue 17:00 ET — closed, but fund NAVs are still posting
        datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc): ("nav", 600),
        # Tue 21:00 ET — well past the NAV window
        datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc): ("closed", 0),
        # Saturday
        datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc): ("closed", 0),
        # Friday 09:00 ET — before the open
        datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc): ("closed", 0),
    }
    for when, (reason, seconds) in cases.items():
        assert cal.refresh_cadence(when, 60, True) == (seconds, reason), when


def test_refresh_cadence_respects_the_off_switch():
    from datetime import datetime, timezone

    from app.services import market_calendar as cal

    mid_session = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
    assert cal.refresh_cadence(mid_session, 0, True) == (0, "off")


def test_sandbox_mode_is_always_live():
    """With ENFORCE_MARKET_HOURS=false everything fills at the latest price
    around the clock, so the displayed price is always live too."""
    from datetime import datetime, timezone

    from app.services import market_calendar as cal

    saturday = datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)
    assert cal.refresh_cadence(saturday, 60, False) == (60, "open")


def test_viewer_count_does_not_multiply_upstream_calls(db, counter, monkeypatch):
    """Many people watching the same holding is still one request per cache
    window — the shared server-side quote cache is the rate limiter, not the
    number of browsers."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "market_data_provider", "synthetic", raising=False)
    store: dict[str, str] = {}

    class _Fake:
        def get(self, k):
            return store.get(k)

        def set(self, k, v, ex=None):
            store[k] = v

        def incr(self, k):
            store[k] = str(int(store.get(k, "0")) + 1)
            return int(store[k])

        def expire(self, k, s):
            pass

    import app.services.market_data as md
    monkeypatch.setattr(md, "get_redis", lambda: _Fake())

    provider_hits = {"n": 0}
    original = md.SyntheticProvider.quote

    def counted(self, ticker):
        provider_hits["n"] += 1
        return original(self, ticker)

    monkeypatch.setattr(md.SyntheticProvider, "quote", counted)

    for _ in range(40):
        market_data.quote("VOO")
    assert provider_hits["n"] == 1, "40 viewers must cost one upstream call"


# ------------------------------------------- price convention across providers

def test_every_provider_is_pinned_to_split_only_adjustment():
    """All providers must agree on what "the close" means.

    A total-return (dividend-adjusted) series marks every past price down by
    the distributions paid since. This engine pays dividends separately as
    cash, so pricing a backtest off that series counts each distribution twice
    — once as extra shares, once as the credit. Measured on a $10,000 VWELX
    buy dated 2015-01-02 it reported $48,724 against a true $21,016.

    It is also what makes a fallback chain safe: two providers on different
    conventions produce a ledger whose cost basis and returns are wrong even
    though each source is individually correct.
    """
    import inspect

    from app.services.market_data import AlpacaProvider, PolygonProvider, YahooProvider

    alpaca = inspect.getsource(AlpacaProvider.history)
    assert '"adjustment": "split"' in alpaca, "Alpaca must not use split+dividend bars"
    assert '"adjustment": "all"' not in alpaca

    # Polygon's `adjusted` flag is splits-only by definition; assert it stays on
    polygon = inspect.getsource(PolygonProvider.history)
    assert '"adjusted": "true"' in polygon

    # Yahoo must read quote.close, never adjclose, as its primary series
    yahoo = inspect.getsource(YahooProvider.history)
    primary = yahoo.split("if not closes:")[0]
    assert 'get("quote")' in primary
    assert 'get("adjclose")' not in primary, "adjclose is a total-return series"


def test_historical_close_is_the_real_traded_price(monkeypatch):
    """A split-adjusted close restates for splits only, so multiplying back by
    the split factor must recover the price that was actually on the screen."""
    from app.services.market_data import YahooProvider

    # AAPL 2015-01-02 closed at $109.33; a 4:1 split followed in Aug 2020.
    payload = {
        "timestamp": [1420223400],
        "indicators": {
            "quote": [{"close": [27.3325]}],
            "adjclose": [{"adjclose": [24.1718]}],
        },
    }
    p = YahooProvider()
    monkeypatch.setattr(p, "_chart", lambda t, params: payload)
    (_, close), = p.history("AAPL", date(2015, 1, 1), date(2015, 1, 3))
    assert close * 4 == Decimal("109.3300"), "must be the traded price, not total return"
