"""Import a Vanguard "Transaction History" export into a PaperTick account.

Usage (from backend/):
    .venv/bin/python -m scripts.import_vanguard --email you@example.com \
        --roth /tmp/rothActivityReport.xlsx \
        --rollover /tmp/rolloverActivityReport.xlsx \
        --brokerage /tmp/brokerageActivityReport.xlsx [--apply] [--replace]

Runs as a dry run unless --apply is given, printing the reconciliation it would
produce. --replace clears anything previously imported into those accounts so a
re-run is idempotent rather than doubling every position.

What the export looks like, and how it maps
-------------------------------------------
Row 4 is the header; rows 5+ are activity; the tail is a disclosure block with
no date. Everything is text ("$1,234.5600", "1,830.9570", "7/22/2019").

The file spans Vanguard's 2019 platform conversion, and the two eras differ:

  legacy (blank "Account type", pre-2019 mutual-fund platform)
      Buy            positive amount, no price  -> price = amount / quantity
      Dividend       carries a quantity         -> income AND its reinvestment
      Exchange       sign lives on the quantity -> one leg out, one leg in
  brokerage (Account type = CASH)
      Buy            negative amount, has price
      Dividend       no quantity; a separate "Reinvestment" row buys the shares
      Sweep in/out   VMFXX settlement moves

Sweeps are dropped: in PaperTick uninvested cash *is* the settlement fund, so
importing them alongside the deposit and the purchase would count the same
dollars twice. TRANSFER TO/FROM rows are kept — they are the two sides of the
platform conversion and net to the right position when replayed in order.

Vanguard's export does not say which tax year a contribution was designated to,
so IRA contributions are attributed to the year of the trade date. A January-to-
April contribution designated to the prior year will land a year late; nothing
else depends on it.
"""

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import (
    Account,
    AccountType,
    Asset,
    AssetCategory,
    AssetClass,
    AssetRegion,
    CashFlowKind,
    Contribution,
    Dividend,
    Order,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    Position,
    QuantityType,
    TaxLot,
    Transaction,
    User,
)
from app.services.trading import execute_fill, q_money, q_price, q_shares

ZERO = Decimal("0")
SETTLEMENT_TICKER = "VMFXX"
IMPORT_MEMO = "Imported from Vanguard activity export"
FUNDING_MEMO = "Funding inferred from Vanguard activity export"

# Symbols that appear in these exports, with the metadata the app shows.
ASSETS = {
    "VTSAX": ("Vanguard Total Stock Market Index Admiral", AssetClass.MUTUAL_FUND,
              "0.0004", AssetCategory.STOCK, AssetRegion.US),
    "VTIVX": ("Vanguard Target Retirement 2045 Fund", AssetClass.MUTUAL_FUND,
              "0.0008", AssetCategory.MIXED, AssetRegion.GLOBAL),
    "VWELX": ("Vanguard Wellington Fund Investor Shares", AssetClass.MUTUAL_FUND,
              "0.0025", AssetCategory.MIXED, AssetRegion.US),
    "VOO":   ("Vanguard S&P 500 ETF", AssetClass.ETF,
              "0.0003", AssetCategory.STOCK, AssetRegion.US),
    "IBIT":  ("iShares Bitcoin Trust ETF", AssetClass.ETF,
              "0.0025", AssetCategory.COMMODITY, AssetRegion.US),
}

BUCKETS = {
    "roth": (AccountType.ROTH_IRA, "Roth IRA"),
    "rollover": (AccountType.ROLLOVER_IRA, "Rollover IRA"),
    "brokerage": (AccountType.TAXABLE, "Taxable Brokerage"),
}

# income rows; a quantity on one of these means it was auto-reinvested
INCOME_TYPES = {"dividend", "capital gain (lt)", "capital gain (st)", "income",
                "interest", "capital gain"}
CASH_IN_TYPES = {"contribution", "funds received", "rollover"}
SKIP_TYPES = {"sweep in", "sweep out"}

# within one day: fund it, then income, then sells, then buys
ORDER_CASH_IN, ORDER_INCOME, ORDER_SELL, ORDER_BUY = 0, 1, 2, 3


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value) -> Decimal | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip()
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned or cleaned == ".":
        return None
    amount = Decimal(cleaned)
    return -amount if negative else amount


class Row:
    """`day` is the trade date — when shares change hands, so it dates the tax
    lot. `settles` is when the cash moves, which is what the replay sequences
    on: Vanguard routinely trades a day before the funding lands, and ordering
    by trade date alone makes a purchase look unfunded when it was not."""

    __slots__ = ("day", "settles", "symbol", "kind", "shares", "price", "amount",
                 "raw_type", "rank")

    def __repr__(self) -> str:
        return f"<{self.day} {self.kind} {self.symbol} {self.shares} @ {self.price} = {self.amount}>"


def read_rows(path: str) -> tuple[list[Row], list[str]]:
    """Parse one export into normalized rows, plus a list of skipped lines."""
    from openpyxl import load_workbook

    ws = load_workbook(path, data_only=True).worksheets[0]
    rows: list[Row] = []
    skipped: list[str] = []
    for raw in ws.iter_rows(min_row=5, values_only=True):
        if not any(c not in (None, "") for c in raw):
            continue
        settle, trade, symbol, _name, raw_type, _acct, qty, price, _fees, amount = raw[:10]
        day = parse_date(trade) or parse_date(settle)
        settles = parse_date(settle) or day
        kind_text = str(raw_type or "").strip()
        if day is None:                      # disclosure footer
            continue
        low = kind_text.lower()
        symbol_text = str(symbol or "").strip().upper()
        if low in SKIP_TYPES or (symbol_text == SETTLEMENT_TICKER and low != "dividend"):
            # sweeps and the money-market reinvestment move cash between the
            # account and VMFXX; here the settlement fund IS the cash balance,
            # so replaying them would move the same dollars twice. Its dividend
            # still comes through as income.
            skipped.append(f"{day} {kind_text} {symbol_text} (settlement-fund internal)")
            continue

        shares = parse_number(qty)
        unit = parse_number(price)
        value = parse_number(amount)
        symbol = (str(symbol or "").strip() or "").upper()

        row = Row()
        row.day, row.settles, row.symbol, row.raw_type = day, settles, symbol, kind_text
        row.shares = abs(shares) if shares is not None else None
        row.price = unit
        row.amount = abs(value) if value is not None else None

        if low.startswith("transfer from"):
            row.kind, row.rank = "buy", ORDER_BUY
        elif low.startswith("transfer to"):
            row.kind, row.rank = "sell", ORDER_SELL
        elif low in CASH_IN_TYPES:
            row.kind, row.rank = "cash_in", ORDER_CASH_IN
        elif low in INCOME_TYPES:
            row.kind, row.rank = "income", ORDER_INCOME
        elif low.startswith("sell"):
            row.kind, row.rank = "sell", ORDER_SELL
        elif low.startswith("buy") or low.startswith("reinvestment"):
            row.kind, row.rank = "buy", ORDER_BUY
        elif low == "exchange":
            # legacy exchange: the sign on the quantity says which leg this is
            if shares is not None and shares < 0:
                row.kind, row.rank = "sell", ORDER_SELL
            else:
                row.kind, row.rank = "buy", ORDER_BUY
        else:
            skipped.append(f"{day} UNRECOGNIZED TYPE {kind_text!r} amount={amount}")
            continue

        if row.kind in ("buy", "sell"):
            if not row.shares or not row.amount:
                skipped.append(f"{day} {kind_text} without shares/amount")
                continue
            # Price is always derived from the amount, never taken from the
            # file: Vanguard rounds shares to 4dp and price to 2-4dp, so
            # shares x price overshoots the stated amount by a cent or two on
            # most buys. The dollars are what actually moved, so they win and
            # the per-share price is recomputed to match.
            row.price = row.amount / row.shares
        elif row.amount is None:
            skipped.append(f"{day} {kind_text} without an amount")
            continue
        elif row.shares and (row.price is None or row.price <= 0):
            # legacy income rows are the payout and its reinvestment in one line
            row.price = row.amount / row.shares
        rows.append(row)

    rows.sort(key=lambda r: (r.settles, r.rank, r.day))
    return rows, skipped


def ensure_assets(db) -> None:
    for ticker, (name, klass, er, category, region) in ASSETS.items():
        asset = db.get(Asset, ticker)
        if asset is None:
            asset = Asset(ticker=ticker, name=name, asset_class=klass)
            db.add(asset)
        asset.name, asset.asset_class = name, klass
        asset.expense_ratio = Decimal(er)
        asset.category, asset.region = category, region
        asset.auto_registered = False
    db.flush()


def ensure_account(db, user: User, bucket: str) -> Account:
    account_type, default_name = BUCKETS[bucket]
    account = db.execute(
        select(Account).where(Account.user_id == user.id, Account.account_type == account_type)
    ).scalar_one_or_none()
    if account is None:
        account = Account(user_id=user.id, account_type=account_type, name=default_name,
                          settlement_balance=ZERO, allow_external_funding=False,
                          sort_order=list(BUCKETS).index(bucket))
        db.add(account)
        db.flush()
        print(f"  created account {default_name}")
    return account


def wipe(db, account: Account) -> None:
    """Remove everything previously imported into this account."""
    order_ids = [o.id for o in db.execute(
        select(Order).where(Order.account_id == account.id)).scalars()]
    for model, field in ((Transaction, Transaction.account_id),
                         (TaxLot, TaxLot.account_id),
                         (Position, Position.account_id),
                         (Dividend, Dividend.account_id),
                         (Contribution, Contribution.account_id)):
        db.query(model).filter(field == account.id).delete(synchronize_session=False)
    if order_ids:
        db.query(Order).filter(Order.account_id == account.id).delete(synchronize_session=False)
    account.settlement_balance = ZERO
    db.flush()


def import_account(db, user: User, account: Account, rows: list[Row],
                   dry_run: bool) -> dict:
    """Replay one file into one account, in date order. Returns a summary."""
    is_ira = account.account_type != AccountType.TAXABLE
    stats = defaultdict(int)
    inferred_funding = ZERO
    rejects: list[str] = []

    for row in rows:
        if row.kind == "cash_in":
            _deposit(db, account, row.settles, row.amount, is_ira, IMPORT_MEMO)
            stats["deposits"] += 1
            continue

        if row.kind == "income":
            amount = q_money(row.amount)
            ticker = row.symbol or "VMFXX"
            existing = db.execute(
                select(Dividend).where(
                    Dividend.account_id == account.id,
                    Dividend.ticker == ticker,
                    Dividend.event_date == row.day,
                )
            ).scalar_one_or_none()
            if existing is not None:      # two payouts same day (ST + LT gains)
                existing.amount = Decimal(existing.amount) + amount
            else:
                db.add(Dividend(
                    account_id=account.id, ticker=ticker, event_date=row.day,
                    per_share=ZERO, shares=ZERO, amount=amount, imported=True,
                ))
            account.settlement_balance = Decimal(account.settlement_balance) + amount
            stats["income"] += 1
            # legacy income rows carry the shares they bought with the payout
            if row.shares:
                _fill(db, account, row, OrderSide.BUY, stats, rejects)
            continue

        side = OrderSide.BUY if row.kind == "buy" else OrderSide.SELL
        if side == OrderSide.BUY:
            need = q_money(row.shares * q_price(row.price))
            short = need - Decimal(account.settlement_balance)
            if short > 0:
                # the legacy platform reported purchases without a funding row
                _deposit(db, account, row.settles, short, is_ira, FUNDING_MEMO)
                inferred_funding += short
                stats["inferred_deposits"] += 1
        _fill(db, account, row, side, stats, rejects)

    db.flush()
    positions = {
        p.ticker: Decimal(p.shares) for p in db.execute(
            select(Position).where(Position.account_id == account.id)).scalars()
    }
    return {
        "stats": dict(stats),
        "cash": Decimal(account.settlement_balance),
        "positions": positions,
        "inferred_funding": inferred_funding,
        "rejects": rejects,
    }


def _deposit(db, account: Account, day: date, amount: Decimal, is_ira: bool, memo: str) -> None:
    amount = q_money(amount)
    db.add(Contribution(
        account_id=account.id,
        tax_year=day.year if is_ira else None,
        amount=amount,
        kind=CashFlowKind.CONTRIBUTION,
        memo=memo,
        timestamp=datetime.combine(day, time(12, 0), tzinfo=timezone.utc),
    ))
    account.settlement_balance = Decimal(account.settlement_balance) + amount


def _fill(db, account: Account, row: Row, side: OrderSide, stats, rejects) -> None:
    """Book one buy/sell through the trading engine so tax lots, positions,
    realized gains and cash all move exactly as they would for a live fill."""
    shares = q_shares(row.shares)
    if shares <= 0:
        return
    if side == OrderSide.SELL:
        # An export is a window on a longer history: the opening position can
        # predate it, so a close-out can name more shares than the replay
        # holds. Sell what is there — the position still ends at zero, which is
        # what the close-out meant — and say so.
        position = db.execute(
            select(Position).where(Position.account_id == account.id,
                                   Position.ticker == row.symbol)
        ).scalar_one_or_none()
        held = q_shares(Decimal(position.shares)) if position else ZERO
        if held <= 0:
            rejects.append(f"{row.day} {row.raw_type} {row.symbol} {shares}: nothing held to sell")
            stats["skipped_sells"] += 1
            return
        if shares > held:
            rejects.append(
                f"{row.day} {row.raw_type} {row.symbol}: named {shares} shares but the "
                f"replayed history holds {held} — sold {held} (opening position predates "
                f"this export)"
            )
            shares = held
            stats["clamped_sells"] += 1
    order = Order(
        account_id=account.id,
        ticker=row.symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity_type=QuantityType.SHARES,
        quantity=shares,
        as_of=row.day,
        status=OrderStatus.PENDING,
        source=OrderSource.API,
    )
    db.add(order)
    db.flush()
    txn = execute_fill(db, order, q_price(row.price), row.day)
    if txn is None:
        rejects.append(f"{row.day} {row.raw_type} {row.symbol} {shares}: {order.reject_reason}")
        stats["rejected"] += 1
    else:
        stats["buys" if side == OrderSide.BUY else "sells"] += 1
    # the fill is dated in the past; stamp the ledger clock to match
    order.created_at = datetime.combine(row.day, time(12, 0), tzinfo=timezone.utc)
    if txn is not None:
        txn.executed_at = order.created_at


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--email", required=True)
    for bucket in BUCKETS:
        ap.add_argument(f"--{bucket}", help=f"path to the {bucket} export")
    ap.add_argument("--apply", action="store_true", help="commit (default is a dry run)")
    ap.add_argument("--replace", action="store_true",
                    help="clear existing activity in those accounts first")
    args = ap.parse_args()

    files = {b: getattr(args, b) for b in BUCKETS if getattr(args, b)}
    if not files:
        ap.error("give at least one of --roth / --rollover / --brokerage")

    db = get_sessionmaker()()
    user = db.execute(select(User).where(User.email == args.email.lower())).scalar_one_or_none()
    if user is None:
        print(f"No user {args.email!r}", file=sys.stderr)
        return 1

    ensure_assets(db)
    print(f"{'APPLY' if args.apply else 'DRY RUN'} — {user.email}\n")
    grand_total = ZERO
    for bucket, path in files.items():
        rows, skipped = read_rows(path)
        account = ensure_account(db, user, bucket)
        if args.replace:
            wipe(db, account)
        result = import_account(db, user, account, rows, not args.apply)

        print(f"{BUCKETS[bucket][1]}  ({path})")
        print(f"  parsed {len(rows)} rows, skipped {len(skipped)}")
        print(f"  {result['stats']}")
        print(f"  settlement fund: ${result['cash']:,.2f}")
        if result["inferred_funding"]:
            print(f"  funding inferred for legacy buys: ${result['inferred_funding']:,.2f}")
        for ticker, shares in sorted(result["positions"].items()):
            print(f"    {ticker:<8} {shares:>16,.4f} shares")
        for line in result["rejects"][:10]:
            print(f"  REJECTED {line}")
        if len(result["rejects"]) > 10:
            print(f"  ... and {len(result['rejects']) - 10} more rejections")
        for line in skipped[:3]:
            print(f"  skipped: {line}")
        if len(skipped) > 3:
            print(f"  ... and {len(skipped) - 3} more skipped rows")
        print()
        grand_total += result["cash"]

    if args.apply:
        db.commit()
        print("committed.")
    else:
        db.rollback()
        print("dry run — nothing written (pass --apply to commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
