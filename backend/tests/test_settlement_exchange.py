"""Settlement fund (VMFXX), exchanges, and timeframe-scoped performance."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Asset,
    AssetClass,
    CashFlowKind,
    Contribution,
    Dividend,
    Order,
    OrderSource,
)
from app.schemas import ExchangeIn, OrderCreateIn
from app.services import metrics, settlement, trading


@pytest.fixture()
def vti_asset(db):
    db.add(Asset(ticker="VTI", name="Vanguard Total Stock Market ETF",
                 asset_class=AssetClass.ETF, expense_ratio=Decimal("0.0003")))
    db.commit()
    return db.get(Asset, "VTI")


# ---------------------------------------------------------------- settlement

def test_settlement_accrual_credits_monthly_dividend(db, taxable):
    taxable.settlement_balance = Decimal("100000")
    taxable.settlement_accrued_through = date(2026, 5, 20)
    db.commit()

    credited = settlement.accrue_account(db, taxable, through=date(2026, 6, 2))
    db.commit()

    # 11 days of May accrue and pay on the 31st; June 1-2 accrue but do not
    rate = settlement.yield_on(date(2026, 5, 31))
    expected = Decimal("100000") * rate / Decimal("365") * 11
    assert abs(credited - expected) < Decimal("0.02")
    assert Decimal(taxable.settlement_balance) == Decimal("100000") + credited

    row = db.query(Dividend).filter(Dividend.ticker == settlement.TICKER).one()
    assert row.event_date == date(2026, 5, 31)
    assert Decimal(row.amount) == credited
    assert taxable.settlement_accrued_through == date(2026, 6, 2)
    assert Decimal(taxable.settlement_accrued) > 0  # June is still accruing


def test_settlement_accrual_is_idempotent(db, taxable):
    taxable.settlement_balance = Decimal("50000")
    taxable.settlement_accrued_through = date(2026, 6, 1)
    db.commit()

    first = settlement.accrue_account(db, taxable, through=date(2026, 6, 30))
    db.commit()
    again = settlement.accrue_account(db, taxable, through=date(2026, 6, 30))
    db.commit()

    assert first > 0
    assert again == 0  # the cursor already covers the period
    assert db.query(Dividend).filter(Dividend.ticker == settlement.TICKER).count() == 1


def test_first_accrual_only_starts_the_clock(db, taxable):
    taxable.settlement_balance = Decimal("10000")
    assert taxable.settlement_accrued_through is None
    assert settlement.accrue_account(db, taxable, through=date(2026, 6, 30)) == 0
    assert taxable.settlement_accrued_through == date(2026, 6, 30)


def test_settlement_fund_is_not_tradable(db, taxable):
    with pytest.raises(HTTPException) as exc:
        trading.place_order(
            db, taxable,
            OrderCreateIn(account_id=taxable.id, ticker=settlement.TICKER, side="BUY",
                          quantity_type="DOLLARS", quantity=Decimal("100")),
            OrderSource.API,
        )
    assert exc.value.status_code == 422
    assert "settlement fund" in exc.value.detail


# ----------------------------------------------------------------- exchanges

def _buy(db, account, ticker, dollars, days_ago=None):
    return trading.place_order(
        db, account,
        OrderCreateIn(
            account_id=account.id, ticker=ticker, side="BUY",
            quantity_type="DOLLARS", quantity=Decimal(dollars),
            as_of=(date.today() - timedelta(days=days_ago)) if days_ago else None,
        ),
        OrderSource.API,
    )


def test_exchange_sells_and_reinvests(db, taxable, vti_asset):
    _buy(db, taxable, "VOO", "5000", days_ago=400)
    before = Decimal(taxable.settlement_balance)

    sell, sell_txn, buy, buy_txn = trading.place_exchange(
        db, taxable,
        ExchangeIn(account_id=taxable.id, from_ticker="VOO", to_ticker="VTI",
                   exchange_all=True),
        OrderSource.API,
    )
    db.commit()

    assert sell.status.value == "FILLED" and sell_txn is not None
    assert buy is not None and buy.status.value == "FILLED" and buy_txn is not None
    assert buy.exchange_from_order_id == sell.id
    assert sell.exchange_to_ticker == "VTI"
    # proceeds went straight into the new holding, not into the settlement fund
    assert abs(Decimal(taxable.settlement_balance) - before) < Decimal("0.01")
    assert Decimal(buy_txn.gross_amount) == Decimal(sell_txn.gross_amount) - Decimal(sell_txn.fees)
    assert sell_txn.realized_gains is not None
    # the whole VOO position is gone; VTI is held
    tickers = {p.ticker for p in metrics.positions_view(db, taxable.user, taxable.id)}
    assert tickers == {"VTI"}


def test_exchange_preview_details_taxable_gains(db, taxable, vti_asset):
    _buy(db, taxable, "VOO", "5000", days_ago=400)
    db.commit()
    orders_before = db.query(Order).count()
    balance_before = Decimal(taxable.settlement_balance)

    preview = trading.preview_exchange(
        db, taxable,
        ExchangeIn(account_id=taxable.id, from_ticker="VOO", to_ticker="VTI",
                   exchange_all=True),
    )
    assert preview.taxable is True
    assert preview.lots and preview.lots[0].term == "LONG"
    assert preview.long_term_gains != 0
    assert preview.short_term_gains == 0
    assert preview.total_gains == preview.short_term_gains + preview.long_term_gains - preview.fees
    assert preview.estimated_shares_bought > 0
    assert any("1099-B" in n for n in preview.notes)
    # a preview is read-only: no order placed, no money moved
    assert db.query(Order).count() == orders_before
    assert Decimal(taxable.settlement_balance) == balance_before


def test_exchange_in_ira_has_no_tax_impact(db, roth, voo_asset, vti_asset):
    roth.settlement_balance = Decimal("10000")
    db.add(Contribution(account_id=roth.id, tax_year=None, amount=Decimal("10000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    _buy(db, roth, "VOO", "5000", days_ago=400)
    db.commit()

    preview = trading.preview_exchange(
        db, roth,
        ExchangeIn(account_id=roth.id, from_ticker="VOO", to_ticker="VTI", exchange_all=True),
    )
    assert preview.taxable is False
    assert any("not taxable events" in n for n in preview.notes)


def test_exchange_rejects_same_symbol(db, taxable):
    with pytest.raises(HTTPException) as exc:
        trading.preview_exchange(
            db, taxable,
            ExchangeIn(account_id=taxable.id, from_ticker="VOO", to_ticker="VOO",
                       exchange_all=True),
        )
    assert exc.value.status_code == 422


# --------------------------------------------------------------- performance

def test_performance_figures_track_the_timeframe(db, user, taxable):
    db.add(Contribution(account_id=taxable.id, tax_year=None, amount=Decimal("10000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    _buy(db, taxable, "VOO", "5000", days_ago=900)
    db.commit()

    all_time = metrics.performance(db, user, None, "all")
    one_month = metrics.performance(db, user, None, "1m")

    # the short window opens on a non-zero balance; the full one starts at zero
    assert all_time.beginning_balance == Decimal("0.00")
    assert one_month.beginning_balance > 0
    assert one_month.period_start > all_time.period_start
    # returns are the identity ending - beginning - flows, per window
    for perf in (all_time, one_month):
        assert perf.investment_returns == (
            perf.ending_balance - perf.beginning_balance - perf.net_cash_flow
        )
    # a sub-year window reports a period return, a multi-year one annualizes
    assert one_month.rate_of_return_annualized is False
    assert all_time.rate_of_return_annualized is True
    assert all_time.rate_of_return_pct != one_month.rate_of_return_pct


def test_account_returns_lists_balance_and_rate(db, user, taxable):
    db.add(Contribution(account_id=taxable.id, tax_year=None, amount=Decimal("10000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()
    _buy(db, taxable, "VOO", "5000", days_ago=400)
    db.commit()

    out = metrics.account_returns(db, user, "1y")
    assert len(out.accounts) == 1
    row = out.accounts[0]
    assert row.account_id == taxable.id
    assert row.balance > 0
    assert row.settlement_balance == Decimal(taxable.settlement_balance).quantize(Decimal("0.01"))
    assert out.total_balance == row.balance


# ------------------------------------------------------------ account admin

def test_accounts_reorder_and_one_per_type(db, user, taxable, roth):
    from fastapi import HTTPException

    from app.deps import Principal
    from app.models import Scenario
    from app.routers.accounts import create_account, list_accounts, reorder_accounts
    from app.schemas import AccountCreateIn, AccountOrderIn

    principal = Principal(user=user, scopes={"read", "trade", "manage"}, via_api_key=False,
                          scenario=db.get(Scenario, taxable.scenario_id))

    before = [a.id for a in list_accounts(principal, db)]
    assert before == [taxable.id, roth.id]

    reorder_accounts(AccountOrderIn(account_ids=[roth.id, taxable.id]), principal, db)
    assert [a.id for a in list_accounts(principal, db)] == [roth.id, taxable.id]

    with pytest.raises(HTTPException) as exc:  # a second brokerage is refused
        create_account(AccountCreateIn(account_type="TAXABLE", name="test2"), principal, db)
    assert exc.value.status_code == 409

    # a type the user does not have yet is fine, and lands at the end
    created = create_account(
        AccountCreateIn(account_type="TRADITIONAL_IRA", name="Rollover-ish"), principal, db
    )
    assert [a.id for a in list_accounts(principal, db)] == [roth.id, taxable.id, created.id]

    with pytest.raises(HTTPException) as exc:  # ids must belong to the user
        reorder_accounts(AccountOrderIn(account_ids=["not-an-account"]), principal, db)
    assert exc.value.status_code == 404
