"""Celery tasks: recurring investments, scheduled orders, limit-order fills.

Each task opens its own session; due rows are claimed with
SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers never double-execute.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db import for_update, get_sessionmaker
from app.models import (
    Asset,
    AssetClass,
    Order,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    QuantityType,
    RecurringRule,
    RuleStatus,
    utcnow,
)
from app.config import get_settings
from app.services import market_calendar as cal
from app.services.market_data import MarketDataError, market_data
from app.services.scheduling import advance_rule, occurrences
from app.services.trading import _close_or_none, _slipped, execute_fill
from app.workers.celery_app import celery

log = logging.getLogger("papertick.worker")


def _contribution_room(db, account_id: str) -> Decimal:
    """Contribution room left in the current tax year for this account's owner,
    or an effectively unlimited amount where no annual limit applies."""
    from app.models import Account
    from app.services import irs

    account = db.get(Account, account_id)
    if account is None:
        return Decimal("0")
    statuses = irs.contribution_statuses(db, account)
    current = [s for s in statuses if not s.is_prior_year]
    if not current:
        return Decimal("0") if statuses or account.account_type.value != "TAXABLE" \
            else Decimal("100000000")
    return Decimal(current[0].remaining)



def _aware(value: datetime | None) -> datetime | None:
    """Timestamps come back from Postgres tz-aware, but not from every backend
    (SQLite has no tz type), and a naive one here would raise mid-comparison
    and take the whole recurring run down with it. Stored times are UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _due_runs(rule: RecurringRule, now: datetime) -> list[datetime]:
    """Every run this rule owes, oldest first.

    Normally one element: the run that just came due. After an outage — or any
    stretch where the worker was not running — it is every occurrence that was
    missed, so a three-day gap on a daily rule replays three buys instead of
    collapsing them into one. Occurrences older than MAX_CATCHUP_DAYS are
    dropped rather than replayed, so restoring an old backup cannot fire years
    of trades at once.
    """
    first = _aware(rule.next_run_at)
    if first is None or first > now:
        return []
    settings = get_settings()
    if not settings.catchup_missed_runs:
        return [first]
    times = [first] + occurrences(
        rule.cadence, rule.day_of_week, rule.day_of_month, rule.month_of_year,
        after=first, until=now,
    )
    floor = now - timedelta(days=settings.max_catchup_days)
    fresh = [t for t in times if t >= floor]
    if len(fresh) < len(times):
        log.warning(
            "rule %s: skipping %d run(s) older than the %d-day catch-up window",
            rule.id, len(times) - len(fresh), settings.max_catchup_days,
        )
    return fresh


def _replay_price(db, rule: RecurringRule, order: Order, when: datetime,
                  now: datetime) -> Decimal | None:
    """Fill price for a run that should have happened at `when`.

    A missed run is priced at the close actually printed on its own day, not
    at today's price: the whole point of catching up is that the ledger ends up
    where it would have been had the system never stopped. Only a run that is
    due right now takes the live quote.
    """
    if when.date() >= now.date():
        asset = db.get(Asset, rule.ticker)
        if asset is not None and asset.asset_class == AssetClass.MUTUAL_FUND:
            return None                      # caller routes funds to NAV pricing
        quote = market_data.quote(rule.ticker)
        return _slipped(quote.price, OrderSide.BUY, order.id)
    # Backfill: the printed close for that day. Funds and equities alike settle
    # on the published number, so no slippage is invented on top of it.
    return _close_or_none(rule.ticker, when.date())


@celery.task
def run_recurring_investments() -> int:
    from app.services.scenarios import frozen_accounts

    db = get_sessionmaker()()
    processed = 0
    try:
        now = utcnow()
        rules = db.execute(
            for_update(
                select(RecurringRule)
                .where(RecurringRule.status == RuleStatus.ACTIVE,
                       RecurringRule.next_run_at <= now,
                       # a deleted scenario is frozen while it is recoverable
                       RecurringRule.account_id.notin_(frozen_accounts(db))),
                skip_locked=True,
            )
        ).scalars().all()
        if rules:
            from app.services.convention import ensure_fresh_for_write

            ensure_fresh_for_write()
            # Same reason as scheduled orders: a recurring buy draws on
            # settlement cash, so any dividend owed to it must be credited
            # before buying power is computed.
            from app.services.dividends import ensure_current

            ensure_current(db, [r.account_id for r in rules])
        for rule in rules:
            runs = _due_runs(rule, now)
            if not runs:
                continue
            stalled_at = None
            for when in runs:
                # A run the outage swallowed is rebuilt at its own date: the
                # order is stamped as placed then, priced at that day's close,
                # and the transaction timestamped to the moment it was due.
                backfill = when.date() < now.date()
                amount = Decimal(rule.amount)
                if rule.fund_to_limit:
                    # "fund to my limit": never contribute past the room left
                    # when the run actually fires, so the final run of the year
                    # lands exactly on the limit even if money went in elsewhere
                    amount = min(amount, _contribution_room(db, rule.account_id))
                    if amount <= 0:
                        log.info("rule %s skipped: %s contribution room is used up",
                                 rule.id, rule.ticker)
                        rule.last_run_at = when
                        processed += 1
                        continue
                order = Order(
                    account_id=rule.account_id,
                    ticker=rule.ticker,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity_type=QuantityType.DOLLARS,
                    quantity=amount,
                    source=OrderSource.RECURRING,
                    recurring_rule_id=rule.id,
                )
                db.add(order)
                db.flush()
                if backfill:
                    order.created_at = when
                    order.as_of = when.date()

                asset = db.get(Asset, rule.ticker)
                is_mf = asset is not None and asset.asset_class == AssetClass.MUTUAL_FUND
                filled = None
                if is_mf and not backfill:
                    # a fund buy due today is forward-priced at tonight's NAV
                    order.nav_date = cal.nav_date_for(now)
                    order.scheduled_for = cal.mf_fill_time(order.nav_date)
                    order.status = OrderStatus.SCHEDULED
                    filled = order  # not a failure: it fills after the close
                else:
                    try:
                        price = _replay_price(db, rule, order, when, now)
                    except MarketDataError as exc:
                        # Every real provider is unreachable. There is no
                        # synthetic substitute by design, so this run is not
                        # failed — it is left where it is and retried, and the
                        # rule stops here so later runs are not skipped past.
                        db.delete(order)
                        stalled_at = when
                        log.warning("recurring rule %s held at %s: market data "
                                    "unavailable (%s)", rule.id, when.isoformat(), exc)
                        break
                    if price is None:
                        order.status = OrderStatus.REJECTED
                        order.reject_reason = (
                            f"No published close for {rule.ticker} on {when.date()}"
                        )
                    else:
                        filled = execute_fill(db, order, price, when.date())
                        if filled is not None and backfill:
                            filled.executed_at = when

                if filled is None:
                    rule.failure_count += 1
                    log.warning("recurring rule %s failed for %s: %s",
                                rule.id, when.isoformat(), order.reject_reason)
                rule.last_run_at = when
                processed += 1

            if stalled_at is not None:
                # Retry exactly this run next tick. Runs already completed above
                # are behind it, so none of them replay.
                rule.next_run_at = stalled_at
                continue

            # Otherwise move forward, even when a run was skipped or rejected:
            # a next_run_at left in the past would replay the same failure on
            # every tick.
            nxt = advance_rule(rule, runs[-1])
            while nxt <= now:
                nxt = advance_rule(rule, nxt)
            rule.next_run_at = nxt
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if processed:
        log.info("recurring investments processed: %d", processed)
    return processed


@celery.task
def run_scheduled_orders() -> int:
    from app.services.trading import run_due_scheduled_orders

    db = get_sessionmaker()()
    try:
        return run_due_scheduled_orders(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def run_limit_orders() -> int:
    from app.services.trading import run_pending_limit_orders

    db = get_sessionmaker()()
    try:
        return run_pending_limit_orders(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def expire_orders() -> int:
    """Lapse resting limit orders past their time-in-force, releasing the cash
    or shares they committed."""
    from app.services.trading import expire_due_orders

    db = get_sessionmaker()()
    try:
        return expire_due_orders(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def accrue_settlement_dividends() -> str:
    """Daily accrual on every account's settlement fund (VMFXX), credited as a
    dividend on the last day of each month."""
    from app.services.settlement import accrue_all

    db = get_sessionmaker()()
    try:
        credited = accrue_all(db)
        db.commit()
        if credited:
            log.info("settlement dividends credited: $%s", credited)
        return str(credited)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def prune_security_events() -> int:
    """Drop security-log rows past the retention window.

    The log is an audit trail, not a permanent record of everywhere someone has
    ever signed in from: it keeps long enough to investigate something noticed
    late, and no longer.
    """
    from app.services.audit import prune

    db = get_sessionmaker()()
    try:
        n = prune(db)
        db.commit()
        if n:
            log.info("pruned %d security event(s)", n)
        return n
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def purge_expired_scenarios() -> int:
    """Destroy deleted scenarios whose retention window has run out."""
    from app.services.scenarios import purge_expired

    db = get_sessionmaker()()
    try:
        purged = purge_expired(db)
        db.commit()
        if purged:
            log.info("purged %d expired scenario(s)", purged)
        return purged
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def verify_price_conventions() -> list[str]:
    """Measure every real provider's price convention against fixed fixtures.

    This is a scheduled upstream call that is not driven by a due order or a
    user — the one other place that is true. It earns it: two requests per
    provider per month buys proof that the prices being written into the
    ledger are on the basis this engine assumes. A vendor silently switching
    to a total-return series is otherwise invisible, and the damage is
    permanent (measured at 2.3x on a VWELX backtest).
    """
    from app.services.convention import probe_all
    from app.services.market_data import market_data

    providers = [p for p in market_data._chain() if p is not market_data.synthetic]
    if not providers:
        # everything is already quarantined; re-probe them so a fixed vendor
        # can earn its way back in
        providers = [market_data.yahoo]
    return [f"{v.provider}={v.status}" for v in probe_all(providers)]


@celery.task
def reconcile_user_dividends(user_id: str) -> str:
    """Bring one user's dividends up to date, off the request path.

    Enqueued when somebody actually opens their portfolio, so the numbers they
    are about to read are current. Throttled to once per account per day inside
    `ensure_current`, so a busy session costs one sweep, not one per page load.
    """
    from app.models import Account
    from app.services.dividends import ensure_current

    db = get_sessionmaker()()
    try:
        ids = [a.id for a in db.query(Account).filter(Account.user_id == user_id)]
        net = ensure_current(db, ids)
        db.commit()
        return str(net)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def reconcile_dividends() -> str:
    from app.services.dividends import reconcile_all

    db = get_sessionmaker()()
    try:
        net = reconcile_all(db)
        db.commit()
        return str(net)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def process_option_expirations() -> int:
    from app.services.options import process_expirations

    db = get_sessionmaker()()
    try:
        return process_expirations(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def generate_statements() -> int:
    """Monthly (1st) statement run: renders any missing monthly and year-end
    statements for every user."""
    from app.models import Scenario, User
    from app.services.statements import generate_missing

    db = get_sessionmaker()()
    created = 0
    try:
        for user in db.execute(select(User).where(User.is_active)).scalars():
            # each scenario keeps its own statement archive
            for scenario in db.execute(
                select(Scenario).where(Scenario.user_id == user.id,
                                       Scenario.deleted_at.is_(None))
            ).scalars():
                created += generate_missing(db, user, scenario_id=scenario.id)
        return created
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery.task
def ensure_irs_limits() -> list[int]:
    """Keeps contribution limits present for the current and next tax year,
    carrying the latest official figures forward as projections."""
    from app.services.irs import ensure_limits

    db = get_sessionmaker()()
    try:
        created = ensure_limits(db)
        if created:
            log.info("IRS limits projected for years: %s", created)
        return created
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
