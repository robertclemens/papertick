"""Dividend accrual by ledger reconciliation.

For every (account, ticker) with transaction history, the expected dividend for
each ex-date is `shares held as of that date × per-share amount`. Rows in the
`dividends` table are reconciled against that expectation and the account's
cash credited/debited by the difference — which makes the process idempotent
and automatically backfills dividends for backdated (backtest) positions and
corrects them if a backdated sell later changes history. Ex-date is used as
the credit date (payment-date lag is not modeled).
"""

import hashlib
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import redis

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import for_update

from app.models import Account, Dividend, OrderSide, Transaction
from app.rate_limit import get_redis
from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.dividends")

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _txns(db: Session, account_id: str, ticker: str) -> list[Transaction]:
    return list(db.execute(
        select(Transaction)
        .where(Transaction.account_id == account_id, Transaction.ticker == ticker)
        .order_by(Transaction.as_of, Transaction.executed_at)
    ).scalars())


def _state(txns: list[Transaction]) -> tuple[Decimal, str]:
    """Shares held now, and a digest of the history that produced them.

    The digest is what makes "nothing changed here" cheap to prove. It covers
    the effective date, side and size of every fill, so a backdated trade
    inserted into the middle of the history changes it even though the row is
    new and its timestamp is old.
    """
    held = ZERO
    h = hashlib.sha256()
    for t in txns:
        shares = Decimal(t.shares_filled)
        held += shares if t.side == OrderSide.BUY else -shares
        h.update(f"{t.as_of}|{t.side.value}|{shares}|".encode())
    return held, h.hexdigest()


def _fingerprint_key(account_id: str, ticker: str) -> str:
    return f"div:fp:{account_id}:{ticker}"


def _settled(account_id: str, ticker: str, held: Decimal, fingerprint: str) -> bool:
    """True when this holding cannot have anything new to reconcile.

    A position that is still open must be re-checked, because a new ex-date can
    be declared any day. A position that has been fully exited cannot: every
    future ex-date sees zero shares and credits zero, and the past rows are
    already correct unless the transaction history itself changed — which the
    fingerprint detects. So a closed holding is fetched exactly once more after
    its final sale and then never again.

    Fails open on a Redis error: the reconcile is idempotent, so the cost of a
    forgotten fingerprint is one redundant fetch, never a wrong balance.
    """
    if held > 0:
        return False
    try:
        return get_redis().get(_fingerprint_key(account_id, ticker)) == fingerprint
    except redis.RedisError:
        return False


def _remember(account_id: str, ticker: str, fingerprint: str) -> None:
    try:
        get_redis().set(_fingerprint_key(account_id, ticker), fingerprint, ex=90 * 86400)
    except redis.RedisError:
        pass


def reconcile_account_ticker(db: Session, account_id: str, ticker: str,
                             events: list | None = None) -> Decimal:
    """Returns the net cash credited (negative = clawed back). Caller commits.

    `events` lets a caller that already fetched this ticker's calendar pass it
    in, so reconciling ten accounts that hold the same fund is one upstream
    request rather than ten.
    """
    txns = _txns(db, account_id, ticker)
    if not txns:
        return ZERO
    if events is None:
        first = min(t.as_of for t in txns)
        try:
            events = market_data.dividends(ticker, first, date.today())
        except MarketDataError:
            return ZERO
    if not events:
        return ZERO

    existing = {
        d.event_date: d
        for d in db.execute(
            select(Dividend).where(Dividend.account_id == account_id, Dividend.ticker == ticker)
        ).scalars()
    }
    # A holding whose payments came from a brokerage export is already the
    # record of what was actually paid. Approximating it from ex-date data
    # would both restate those rows and invent payments for dates the export
    # deliberately does not contain, so the whole holding is left alone.
    if any(row.imported for row in existing.values()):
        return ZERO

    net = ZERO
    for ev_date, per_share in events:
        held = ZERO
        for t in txns:
            if t.as_of > ev_date:
                break
            delta = Decimal(t.shares_filled)
            held += delta if t.side == OrderSide.BUY else -delta
        expected = (held * per_share).quantize(CENT, ROUND_HALF_UP) if held > 0 else ZERO
        row = existing.get(ev_date)
        current = Decimal(row.amount) if row is not None else ZERO
        diff = expected - current
        if diff == 0:
            continue
        if row is None:
            db.add(Dividend(
                account_id=account_id, ticker=ticker, event_date=ev_date,
                per_share=per_share, shares=held, amount=expected,
            ))
        elif expected <= 0:
            db.delete(row)
        else:
            row.per_share = per_share
            row.shares = held
            row.amount = expected
        net += diff

    if net != 0:
        account = db.execute(
            for_update(select(Account).where(Account.id == account_id))
        ).scalar_one()
        account.settlement_balance = Decimal(account.settlement_balance) + net
        log.info("dividends reconciled %s/%s: %+.2f", account_id, ticker, net)
    return net


# ---------------------------------------------------------------- freshness

def _day_key(account_id: str) -> str:
    return f"div:day:{account_id}"


def _needs_refresh(account_id: str, today: str) -> bool:
    """Has this account been reconciled today? Fails open."""
    try:
        return get_redis().get(_day_key(account_id)) != today
    except redis.RedisError:
        return True


def _mark_refreshed(account_id: str, today: str) -> None:
    try:
        get_redis().set(_day_key(account_id), today, ex=3 * 86400)
    except redis.RedisError:
        pass


def ensure_current(db: Session, account_ids) -> Decimal:
    """Bring these accounts' dividends up to date, at most once a day each.

    This is what replaced the unconditional daily sweep. Reconciliation is a
    pure function of the transaction history and the ex-date calendar, so it
    produces the same answer whenever it runs — which means it does not need to
    run on the day of the ex-date, only before somebody or something depends on
    the balance. Callers are therefore the two real dependants: an order about
    to execute, and a user about to look.

    Ex-dates have day granularity, so once per account per day is the finest
    resolution that can discover anything.
    """
    today = date.today().isoformat()
    due = [a for a in dict.fromkeys(account_ids) if a and _needs_refresh(a, today)]
    if not due:
        return ZERO
    # Splits first, always. Yahoo reports historical dividend amounts on the
    # post-split basis, so crediting them against pre-split share counts
    # understates every distribution by exactly the split ratio.
    from app.services.splits import ensure_current as apply_splits

    apply_splits(db, due)
    total = reconcile_scope(db, due)
    for account_id in due:
        _mark_refreshed(account_id, today)
    return total


def reconcile_all(db: Session) -> Decimal:
    """Reconcile every account/ticker pair that has ever traded, skipping
    accounts in deleted scenarios. Caller commits.

    This is the one scheduled task that is not driven by something falling due,
    so it is also the one that can spend the upstream budget on an idle
    deployment. Two things keep it honest:

      - holdings that are fully closed and unchanged since the last run are
        skipped outright (see `_settled`), so a portfolio that has exited a
        fund stops asking about it;
      - the remaining work is grouped by ticker and fetched once over the
        widest window any account needs, so ten accounts holding the same fund
        cost one request rather than ten.

    What is left is genuinely as-needed: one calendar lookup per distinct
    security somebody still holds, once a day.
    """
    from app.services.scenarios import frozen_accounts

    account_ids = [
        a for (a,) in db.execute(
            select(Account.id).where(Account.id.notin_(frozen_accounts(db)))
        ).all()
    ]
    return reconcile_scope(db, account_ids)


def reconcile_scope(db: Session, account_ids: list[str]) -> Decimal:
    """Reconcile just these accounts. Caller commits."""
    from app.services.scenarios import frozen_accounts

    if not account_ids:
        return ZERO
    pairs = db.execute(
        select(Transaction.account_id, Transaction.ticker)
        .where(Transaction.account_id.in_(account_ids),
               Transaction.account_id.notin_(frozen_accounts(db)))
        .distinct()
    ).all()

    work: dict[str, list[tuple[str, list[Transaction], str]]] = {}
    skipped = 0
    for account_id, ticker in pairs:
        txns = _txns(db, account_id, ticker)
        if not txns:
            continue
        held, fingerprint = _state(txns)
        if _settled(account_id, ticker, held, fingerprint):
            skipped += 1
            continue
        work.setdefault(ticker, []).append((account_id, txns, fingerprint))

    total = ZERO
    fetched = 0
    for ticker, items in work.items():
        first = min(min(t.as_of for t in txns) for _, txns, _ in items)
        try:
            events = market_data.dividends(ticker, first, date.today())
        except MarketDataError:
            continue
        fetched += 1
        for account_id, txns, fingerprint in items:
            total += reconcile_account_ticker(db, account_id, ticker, events=events)
            _remember(account_id, ticker, fingerprint)

    log.info("dividend reconcile: %d holdings, %d skipped as settled, "
             "%d upstream calendar lookups", len(pairs), skipped, fetched)
    return total
