"""Applying stock splits to holdings.

A split is the one corporate action that changes a position without any order
behind it, and it is the one that fails silently if ignored. The price series
is restated onto the new basis the moment the split takes effect — that is what
"split-adjusted" means — so a holding whose share count is *not* restated keeps
its old share count against the new, lower price. NVDA's 10:1 in June 2024
would have taken a $100,000 position to $10,000 overnight, with no error, no
rejected order and nothing in the ledger to explain it.

Applying one is destructive: every open lot's share count and per-share basis
is rewritten. Total basis is preserved exactly — the shares multiply by the
ratio and the cost per share divides by it — so realized gains, holding
periods and wash-sale windows are all untouched. Acquisition dates are
deliberately left alone: a split does not restart a holding period.

One subtlety decides correctness here. Historical prices in this engine are
split-adjusted, so a *backdated* purchase is filled at a price already restated
for every split since — its share count is post-split from the start, and
applying the split again would invent shares. What matters is therefore when
the lot row was written, not the date it is effective for.

Idempotency is enforced by the database, not by a flag: `split_applications`
is unique on (account, ticker, ex-date), so a second attempt is refused rather
than doubling the position.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import for_update
from app.models import Position, SplitApplication, TaxLot, Transaction
from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.splits")

ZERO = Decimal("0")
# Match the column precision exactly (Numeric(20,6) / Numeric(18,6)). Rounding
# the per-share basis to fewer places than the column holds loses money on
# every split: at 4dp a 10-for-1 on a $75,000 lot drifted the total basis by
# three cents, and basis drift compounds through every later sale and every
# realized gain it feeds.
SHARE_Q = Decimal("0.000001")
PRICE_Q = Decimal("0.000001")


def _entered_on(lot: TaxLot) -> date:
    """When this lot was actually written, not the date it is effective for.

    A backdated ("as of") fill has an `acquired_on` far in the past but was
    priced today, from a series already restated for every split since. Only
    `created_at` says which side of a split the *price* came from.
    """
    created = getattr(lot, "created_at", None)
    if created is None:
        return lot.acquired_on
    return created.date()


def _applied(db: Session, account_id: str, ticker: str) -> set[date]:
    return {
        d for (d,) in db.execute(
            select(SplitApplication.event_date).where(
                SplitApplication.account_id == account_id,
                SplitApplication.ticker == ticker,
            )
        ).all()
    }


def apply_for(db: Session, account_id: str, ticker: str) -> int:
    """Apply any split this holding has lived through but not yet been given.

    Only splits dated at or after the first lot's acquisition matter: a split
    that happened before the shares were bought is already reflected in the
    price that was paid.
    """
    lots = db.execute(
        for_update(
            select(TaxLot)
            .where(TaxLot.account_id == account_id, TaxLot.ticker == ticker)
            .order_by(TaxLot.acquired_on)
        )
    ).scalars().all()
    open_lots = [l for l in lots if Decimal(l.shares_open) > 0]
    if not open_lots:
        return 0

    first_held = min(l.acquired_on for l in open_lots)
    try:
        events = market_data.splits(ticker, first_held, date.today())
    except MarketDataError:
        return 0
    if not events:
        return 0

    done = _applied(db, account_id, ticker)
    applied = 0
    for event_date, ratio in events:
        if event_date in done or ratio <= 0 or ratio == 1:
            continue
        # The test is when the lot ROW was written, not the date it is
        # effective for. Historical prices here are split-adjusted — restated
        # onto today's basis — so a backdated buy is filled at a price that
        # already reflects every split since, and its share count is already
        # post-split. Applying the split again would multiply shares that were
        # never pre-split.
        #
        # Only a lot created before the ex-date was priced in the pre-split
        # world and therefore needs restating. (Observed live: a SCHD lot
        # dated 2024-08-29 but entered 2026-08-29 was tripled by the wrong
        # test before this.)
        affected = [
            l for l in open_lots
            if Decimal(l.shares_open) > 0 and _entered_on(l) < event_date
        ]
        if not affected:
            continue

        before = sum((Decimal(l.shares_open) for l in affected), ZERO)
        for lot in affected:
            shares = Decimal(lot.shares_open)
            cost = Decimal(lot.cost_per_share)
            lot.shares_open = (shares * ratio).quantize(SHARE_Q)
            # total basis is invariant: shares x ratio, cost / ratio
            lot.cost_per_share = (cost / ratio).quantize(PRICE_Q)
        after = sum((Decimal(l.shares_open) for l in affected), ZERO)

        db.add(SplitApplication(
            account_id=account_id, ticker=ticker, event_date=event_date,
            ratio=ratio, shares_before=before, shares_after=after,
        ))
        try:
            db.flush()
        except IntegrityError:
            # Another process applied the same split between our read and our
            # write. The unique key is the authority; undo and move on.
            db.rollback()
            log.info("split %s %s already applied concurrently", ticker, event_date)
            return applied
        applied += 1
        log.warning("applied %s-for-1 split of %s on %s to account %s: "
                    "%s shares -> %s", ratio, ticker, event_date, account_id,
                    before, after)

    if applied:
        _resync(db, account_id, ticker, lots)
    return applied


def _resync(db: Session, account_id: str, ticker: str, lots) -> None:
    """Rebuild the position row from the restated lots."""
    live = [l for l in lots if Decimal(l.shares_open) > 0]
    total = sum((Decimal(l.shares_open) for l in live), ZERO)
    position = db.execute(
        for_update(
            select(Position).where(Position.account_id == account_id,
                                   Position.ticker == ticker)
        )
    ).scalar_one_or_none()
    if position is None:
        return
    if total <= 0:
        db.delete(position)
        return
    basis = sum((Decimal(l.shares_open) * Decimal(l.cost_per_share) for l in live), ZERO)
    position.shares = total.quantize(SHARE_Q)
    position.average_cost = (basis / total).quantize(PRICE_Q) if total else ZERO


def ensure_current(db: Session, account_ids) -> int:
    """Apply outstanding splits for every holding in these accounts.

    Runs at the same demand-driven moments as dividend reconciliation, and
    always *before* it: Yahoo reports historical dividend amounts on the
    post-split basis, so crediting dividends against pre-split share counts
    understates them by exactly the split ratio.
    """
    ids = [a for a in dict.fromkeys(account_ids) if a]
    if not ids:
        return 0
    pairs = db.execute(
        select(Transaction.account_id, Transaction.ticker)
        .where(Transaction.account_id.in_(ids))
        .distinct()
    ).all()
    return sum(apply_for(db, account_id, ticker) for account_id, ticker in pairs)
