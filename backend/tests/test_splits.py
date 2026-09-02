"""Splits applied to holdings.

A split changes a position with no order behind it, and ignoring one fails
silently: the price series is restated onto the new basis while the share
count is not, so the holding loses exactly the value of the split.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import Position, SplitApplication, TaxLot
from app.schemas import OrderCreateIn
from app.models import OrderSide, OrderSource
from app.services import splits, trading


@pytest.fixture()
def held(db, taxable, scenario, monkeypatch):
    """A position genuinely held through a 10-for-1 split.

    "Genuinely" is the point: the lot row must have been written before the
    ex-date, because that is what says its fill price came from the pre-split
    world. A merely backdated buy is priced from the already-restated series
    and must not be adjusted again — covered separately below.
    """
    split_day = date.today() - timedelta(days=30)
    monkeypatch.setattr(
        "app.services.market_data.market_data.splits",
        lambda t, s, e: [(split_day, Decimal("10"))] if t == "VOO" else [],
    )
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side="BUY",
                      quantity_type="SHARES", quantity=Decimal("100")),
        OrderSource.API,
    )
    db.commit()
    lot = db.query(TaxLot).filter(TaxLot.account_id == taxable.id).one()
    lot.created_at = lot.created_at - timedelta(days=90)   # entered pre-split
    lot.acquired_on = split_day - timedelta(days=90)
    db.commit()
    return taxable


def test_a_split_multiplies_shares_and_preserves_total_basis(db, held):
    lot = db.query(TaxLot).filter(TaxLot.account_id == held.id).one()
    before_shares = Decimal(lot.shares_open)
    before_basis = before_shares * Decimal(lot.cost_per_share)

    assert splits.apply_for(db, held.id, "VOO") == 1
    db.commit()

    lot = db.query(TaxLot).filter(TaxLot.account_id == held.id).one()
    after_shares = Decimal(lot.shares_open)
    after_basis = after_shares * Decimal(lot.cost_per_share)

    assert after_shares == before_shares * 10
    # total basis is invariant — a split is not a gain or a loss
    assert abs(after_basis - before_basis) < Decimal("0.01")


def test_the_position_row_follows_the_lots(db, held):
    splits.apply_for(db, held.id, "VOO")
    db.commit()
    pos = db.query(Position).filter(Position.account_id == held.id).one()
    lots = db.query(TaxLot).filter(TaxLot.account_id == held.id).all()
    assert Decimal(pos.shares) == sum(Decimal(l.shares_open) for l in lots)
    assert Decimal(pos.shares) == Decimal("1000.000000")


def test_applying_twice_does_not_double_the_position(db, held):
    """The database key is the guarantee, not a flag someone might forget."""
    assert splits.apply_for(db, held.id, "VOO") == 1
    db.commit()
    shares = Decimal(db.query(Position).filter(Position.account_id == held.id).one().shares)

    for _ in range(4):
        assert splits.apply_for(db, held.id, "VOO") == 0
        db.commit()
    assert Decimal(
        db.query(Position).filter(Position.account_id == held.id).one().shares
    ) == shares
    assert db.query(SplitApplication).filter(
        SplitApplication.account_id == held.id).count() == 1


def test_a_split_before_the_shares_were_bought_is_not_applied(db, taxable, scenario, monkeypatch):
    """A split that predates the purchase is already in the price that was
    paid; applying it would invent shares."""
    monkeypatch.setattr(
        "app.services.market_data.market_data.splits",
        lambda t, s, e: [(date.today() - timedelta(days=400), Decimal("4"))],
    )
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side="BUY",
                      quantity_type="SHARES", quantity=Decimal("50"),
                      as_of=date.today() - timedelta(days=30)),
        OrderSource.API,
    )
    db.commit()
    assert splits.apply_for(db, taxable.id, "VOO") == 0
    assert Decimal(
        db.query(Position).filter(Position.account_id == taxable.id).one().shares
    ) == Decimal("50.000000")


def test_the_holding_period_survives_a_split(db, held):
    """A split does not restart the clock: long-term gains stay long-term."""
    before = {l.acquired_on for l in db.query(TaxLot).filter(TaxLot.account_id == held.id)}
    splits.apply_for(db, held.id, "VOO")
    db.commit()
    after = {l.acquired_on for l in db.query(TaxLot).filter(TaxLot.account_id == held.id)}
    assert before == after


def test_value_is_preserved_across_a_split(db, held, monkeypatch):
    """The regression this exists to prevent: NVDA's 10:1 taking a $100,000
    position to $10,000 with nothing in the ledger to explain it."""
    lot = db.query(TaxLot).filter(TaxLot.account_id == held.id).one()
    shares_before = Decimal(lot.shares_open)
    price_before = Decimal("1000")
    value_before = shares_before * price_before

    splits.apply_for(db, held.id, "VOO")
    db.commit()

    pos = db.query(Position).filter(Position.account_id == held.id).one()
    price_after = price_before / 10          # the restated series
    assert Decimal(pos.shares) * price_after == value_before


def test_a_backdated_buy_is_not_split_again(db, taxable, scenario, monkeypatch):
    """The trap this class of bug lives in.

    Historical prices here are split-adjusted, so a buy dated before a split
    but *entered* after it is already filled on the post-split basis. Applying
    the split again multiplies shares that were never pre-split. Observed live
    on a SCHD lot dated 2024-08-29 and entered 2026-08-29: 189.42 shares were
    tripled to 568.26."""
    split_day = date.today() - timedelta(days=60)
    monkeypatch.setattr(
        "app.services.market_data.market_data.splits",
        lambda t, s, e: [(split_day, Decimal("3"))],
    )
    # effective well before the split, but entered now
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side="BUY",
                      quantity_type="SHARES", quantity=Decimal("100"),
                      as_of=split_day - timedelta(days=30)),
        OrderSource.API,
    )
    db.commit()

    assert splits.apply_for(db, taxable.id, "VOO") == 0, \
        "a backdated fill is already on the post-split basis"
    assert Decimal(
        db.query(Position).filter(Position.account_id == taxable.id).one().shares
    ) == Decimal("100.000000")


def test_a_lot_entered_before_the_split_is_still_adjusted(db, taxable, scenario, monkeypatch):
    """The other side: a position genuinely held through a split must be
    restated, or it silently loses the value of the split."""
    from app.models import TaxLot as _Lot

    split_day = date.today() - timedelta(days=10)
    monkeypatch.setattr(
        "app.services.market_data.market_data.splits",
        lambda t, s, e: [(split_day, Decimal("4"))],
    )
    trading.place_order(
        db, taxable,
        OrderCreateIn(account_id=taxable.id, ticker="VOO", side="BUY",
                      quantity_type="SHARES", quantity=Decimal("25")),
        OrderSource.API,
    )
    db.commit()
    # the lot was really entered before the split
    lot = db.query(_Lot).filter(_Lot.account_id == taxable.id).one()
    lot.created_at = lot.created_at.replace(year=lot.created_at.year - 1)
    db.commit()

    assert splits.apply_for(db, taxable.id, "VOO") == 1
    db.commit()
    assert Decimal(
        db.query(Position).filter(Position.account_id == taxable.id).one().shares
    ) == Decimal("100.000000")


# ------------------------------------- the one automated use of the oracle

def _stuck_nav_order(db, account, nav_date):
    from app.models import Order, OrderStatus, OrderType, QuantityType
    from app.services import market_calendar as cal

    order = Order(
        account_id=account.id, ticker="VWELX", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity_type=QuantityType.DOLLARS,
        quantity=Decimal("500"), status=OrderStatus.SCHEDULED,
        nav_date=nav_date, scheduled_for=cal.mf_fill_time(nav_date),
        source=OrderSource.API,
    )
    db.add(order)
    db.commit()
    return order


def test_a_stuck_fund_order_is_held_when_the_nav_provably_exists(
    db, taxable, scenario, monkeypatch
):
    """Our providers failing is not the same as the fund not pricing.

    Rejecting a trade the user asked for, because our own data source is
    broken, is the wrong call — and only an independent source can tell the
    two apart."""
    from datetime import datetime, timezone
    from app.models import OrderStatus
    from app.services import market_calendar as cal
    from app.services import trading as t

    nav_date = date.today() - timedelta(days=10)
    order = _stuck_nav_order(db, taxable, nav_date)

    monkeypatch.setattr(t.market_data, "close_exact", lambda *a, **k: None)
    monkeypatch.setattr(t, "_close_or_none", lambda *a, **k: None)
    monkeypatch.setattr("app.services.oracle.reference_close",
                        lambda tk, on: Decimal("47.43"))

    t.run_due_scheduled_orders(db, now=cal.mf_fill_time(nav_date) + timedelta(days=2))
    db.refresh(order)
    assert order.status == OrderStatus.SCHEDULED, "must not reject a NAV that exists"


def test_a_stuck_fund_order_is_rejected_when_no_source_has_the_nav(
    db, taxable, scenario, monkeypatch
):
    """And when neither source has it, rejecting is justified and says so."""
    from app.models import OrderStatus
    from app.services import market_calendar as cal
    from app.services import trading as t

    nav_date = date.today() - timedelta(days=10)
    order = _stuck_nav_order(db, taxable, nav_date)

    monkeypatch.setattr(t.market_data, "close_exact", lambda *a, **k: None)
    monkeypatch.setattr(t, "_close_or_none", lambda *a, **k: None)
    monkeypatch.setattr("app.services.oracle.reference_close", lambda tk, on: None)

    t.run_due_scheduled_orders(db, now=cal.mf_fill_time(nav_date) + timedelta(days=2))
    db.refresh(order)
    assert order.status == OrderStatus.REJECTED
    assert "independent source" in order.reject_reason


def test_a_held_order_is_not_held_forever(db, taxable, scenario, monkeypatch):
    """The hold is bounded: past the cap it is rejected even though the NAV
    exists, naming our providers as the failure rather than the fund."""
    from app.config import get_settings
    from app.models import OrderStatus
    from app.services import market_calendar as cal
    from app.services import trading as t

    nav_date = date.today() - timedelta(days=40)
    order = _stuck_nav_order(db, taxable, nav_date)

    monkeypatch.setattr(t.market_data, "close_exact", lambda *a, **k: None)
    monkeypatch.setattr(t, "_close_or_none", lambda *a, **k: None)
    monkeypatch.setattr("app.services.oracle.reference_close",
                        lambda tk, on: Decimal("47.43"))

    cap = get_settings().nav_hold_max_days
    t.run_due_scheduled_orders(
        db, now=cal.mf_fill_time(nav_date) + timedelta(days=cap + 1))
    db.refresh(order)
    assert order.status == OrderStatus.REJECTED
    assert "no provider returned it" in order.reject_reason
