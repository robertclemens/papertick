"""The Vanguard activity importer's cash handling.

These fix a bug that silently corrupted a real ledger: a purchase the balance
could not cover was booked as an external deposit sized to the shortfall, so
the money arrived twice when the deposit that actually paid for it was imported
two days later. The rows below are the ones that caused it.
"""

from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.models import Account, AccountType, Asset, AssetClass, Contribution, Position
from scripts.import_vanguard import import_account, read_rows

HEADER = ["Settlement date", "Trade date", "Symbol", "Name", "Type",
          "Account type", "Quantity", "Price", "Commission & fees**", "Amount"]


def build(tmp_path, rows) -> str:
    """An export: three preamble lines, the header on row 4, activity below."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Custom report"]); ws.append(["Range"]); ws.append([])
    ws.append(HEADER)
    for r in rows:
        ws.append(list(r))
    path = tmp_path / "activity.xlsx"
    wb.save(path)
    return str(path)


@pytest.fixture()
def brokerage(db, user, scenario):
    for ticker in ("VOO",):
        if db.get(Asset, ticker) is None:
            db.add(Asset(ticker=ticker, name=ticker, asset_class=AssetClass.ETF,
                         expense_ratio=Decimal("0.0003")))
    a = Account(user_id=user.id, scenario_id=scenario.id, account_type=AccountType.TAXABLE,
                name="Brokerage", settlement_balance=Decimal("0"))
    db.add(a)
    db.commit()
    return a


# The March 2026 episode, verbatim: Vanguard's ACH platform was down, the
# purchase settled against $1.60 of available cash, and the $500.00 that
# arrived two days later cleared the $248.39 debit — which is why it swept
# into the settlement fund as $251.61 rather than $500.00.
ACH_OUTAGE = [
    ("3/3/2026",  "3/3/2026",  "",      "To: BANK",  "Funds Received", "CASH", "", "", "", "$250.0000"),
    ("3/4/2026",  "3/3/2026",  "VOO",   "S&P 500",   "Buy",            "CASH", "0.4021", "$621.7043", "", "-$249.9900"),
    ("3/11/2026", "3/11/2026", "VMFXX", "Settlement","Dividend",       "CASH", "", "", "", "$0.0300"),
    ("3/11/2026", "3/11/2026", "VMFXX", "Settlement","Sweep out",      "CASH", "", "", "", "$1.5700"),
    ("3/11/2026", "3/10/2026", "VOO",   "S&P 500",   "Buy",            "CASH", "0.4021", "$621.7043", "", "-$249.9900"),
    ("3/13/2026", "3/13/2026", "VMFXX", "Settlement","Sweep in",       "CASH", "", "", "", "-$251.6100"),
    ("3/13/2026", "3/13/2026", "",      "To: BANK",  "Funds Received", "CASH", "", "", "", "$500.0000"),
]


def test_settlement_credit_is_not_an_external_deposit(db, brokerage, tmp_path):
    """The debit is repaid out of the next deposit, not invented alongside it."""
    rows, _ = read_rows(build(tmp_path, ACH_OUTAGE))
    result = import_account(db, brokerage.user_id, brokerage, rows, dry_run=True)

    deposits = db.query(Contribution).filter_by(account_id=brokerage.id).all()
    assert [Decimal(c.amount) for c in deposits] == [Decimal("250.00"), Decimal("500.00")], \
        "only the two real Funds Received rows are deposits"
    assert not result["inferred"], "the file pays for itself; nothing should be inferred"
    assert result["stats"]["credit_extended"] == 1

    # 250.00 + 0.03 + 500.00 - 249.99 - 249.99, i.e. Vanguard's $251.61 sweep
    # less the second purchase.
    assert Decimal(brokerage.settlement_balance) == Decimal("250.05")


def test_deposit_is_never_sized_to_the_shortfall(db, brokerage, tmp_path):
    """A shortfall is a floor on the missing deposit, never its value.

    Before the fix this booked $248.39 — whatever left the balance at exactly
    zero — losing the $1.61 remainder of a real $250.00 deposit for good.
    """
    unfunded = [r for r in ACH_OUTAGE if r[4] != "Funds Received"]
    rows, _ = read_rows(build(tmp_path, unfunded))
    result = import_account(db, brokerage.user_id, brokerage, rows, dry_run=True)

    assert result["inferred"], "cash really is absent here, so it must be reported"
    total = sum(a for _, _, a in result["inferred"])
    assert total == Decimal("499.95")
    assert Decimal(brokerage.settlement_balance) == Decimal("0.00"), \
        "the inferred deposit repays the debit; it does not credit cash twice"


def test_sweeps_never_move_cash_twice(db, brokerage, tmp_path):
    """Here the settlement fund IS the cash balance, so sweeps are not activity."""
    rows, skipped = read_rows(build(tmp_path, ACH_OUTAGE))
    assert not any(r.raw_type in ("Sweep in", "Sweep out") for r in rows)
    assert len(skipped) == 2


def test_shares_and_price_come_from_the_dollars_that_moved(db, brokerage, tmp_path):
    """Vanguard rounds quantity to 4dp, so quantity x price overshoots the
    stated amount. The dollars win and the price is re-derived."""
    rows, _ = read_rows(build(tmp_path, ACH_OUTAGE))
    import_account(db, brokerage.user_id, brokerage, rows, dry_run=True)
    position = db.query(Position).filter_by(account_id=brokerage.id, ticker="VOO").one()
    assert Decimal(position.shares) == Decimal("0.804200")


# ---------------------------------------------------------- tax-year designation

@pytest.fixture()
def roth_account(db, user, scenario):
    if db.get(Asset, "VOO") is None:
        db.add(Asset(ticker="VOO", name="VOO", asset_class=AssetClass.ETF,
                     expense_ratio=Decimal("0.0003")))
    a = Account(user_id=user.id, scenario_id=scenario.id, account_type=AccountType.ROTH_IRA,
                name="Roth", settlement_balance=Decimal("0"))
    db.add(a)
    db.commit()
    return a


# A payroll contribution made on the last business day of the year settles in
# the first days of the next one. Both dates are in the export; only one of
# them designates the tax year.
YEAR_BOUNDARY = [
    ("1/2/2025",  "12/30/2024", "", "To: BANK", "Contribution", "CASH", "", "", "", "$125.0000"),
    ("1/8/2025",  "1/8/2025",   "", "To: BANK", "Contribution", "CASH", "", "", "", "$100.0000"),
]


def test_tax_year_comes_from_the_trade_date_not_settlement(db, user, roth_account, tmp_path, limits):
    """The bug that shifted a year of contributions one bucket forward.

    Booking the 30 December contribution as tax year 2025 because the cash
    landed on 2 January is what made every year carry one payment too many, and
    the last year on record tip over the annual limit.
    """
    rows, _ = read_rows(build(tmp_path, YEAR_BOUNDARY))
    import_account(db, user, roth_account, rows, dry_run=True)

    by_year = {c.tax_year: Decimal(c.amount)
               for c in db.query(Contribution).filter_by(account_id=roth_account.id).all()}
    assert by_year == {2024: Decimal("125.00"), 2025: Decimal("100.00")}


def test_a_rollover_row_is_not_a_contribution(db, user, roth_account, tmp_path, limits):
    """The export names the movement; a rollover consumes no annual room."""
    from app.models import CashFlowKind
    from app.services import irs

    rows, _ = read_rows(build(tmp_path, [
        ("1/8/2025", "1/8/2025", "", "From: 401k", "Rollover",     "CASH", "", "", "", "$40000.0000"),
        ("1/9/2025", "1/9/2025", "", "To: BANK",   "Contribution", "CASH", "", "", "", "$500.0000"),
    ]))
    import_account(db, user, roth_account, rows, dry_run=True)

    rolled = db.query(Contribution).filter_by(
        account_id=roth_account.id, kind=CashFlowKind.ROLLOVER).one()
    assert Decimal(rolled.amount) == Decimal("40000.00")
    assert rolled.tax_year is None
    # ...and it left the year's contribution room untouched
    assert irs.contributed_for_year(
        db, user, 2025, roth_account.scenario_id) == Decimal("500.00")
