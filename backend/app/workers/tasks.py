"""Celery tasks: recurring investments, scheduled orders, limit-order fills.

Each task opens its own session; due rows are claimed with
SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers never double-execute.
"""

import logging
from decimal import Decimal

from sqlalchemy import select

from app.db import get_sessionmaker
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
from app.services import market_calendar as cal
from app.services.market_data import MarketDataError, market_data
from app.services.scheduling import advance_rule
from app.services.trading import _slipped, execute_fill
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


@celery.task
def run_recurring_investments() -> int:
    from app.services.scenarios import frozen_accounts

    db = get_sessionmaker()()
    processed = 0
    try:
        now = utcnow()
        rules = db.execute(
            select(RecurringRule)
            .where(RecurringRule.status == RuleStatus.ACTIVE,
                   RecurringRule.next_run_at <= now,
                   # a deleted scenario is frozen while it is recoverable
                   RecurringRule.account_id.notin_(frozen_accounts(db)))
            .with_for_update(skip_locked=True)
        ).scalars().all()
        for rule in rules:
            amount = Decimal(rule.amount)
            if rule.fund_to_limit:
                # "fund to my limit": never contribute past the room left when
                # the run actually fires, so the final run of the year lands
                # exactly on the limit even if money went in elsewhere
                amount = min(amount, _contribution_room(db, rule.account_id))
                if amount <= 0:
                    log.info("rule %s skipped: %s contribution room is used up",
                             rule.id, rule.ticker)
                    rule.last_run_at = now
                    rule.next_run_at = advance_rule(rule, now)
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
            asset = db.get(Asset, rule.ticker)
            if asset is not None and asset.asset_class == AssetClass.MUTUAL_FUND:
                # fund buys are forward-priced at that day's closing NAV
                order.nav_date = cal.nav_date_for(now)
                order.scheduled_for = cal.mf_fill_time(order.nav_date)
                order.status = OrderStatus.SCHEDULED
                txn = order  # not a failure
            else:
                try:
                    quote = market_data.quote(rule.ticker)
                    txn = execute_fill(db, order, _slipped(quote.price, OrderSide.BUY), now.date())
                except MarketDataError as exc:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = f"Market data unavailable: {exc}"
                    txn = None
            if txn is None:
                rule.failure_count += 1
                log.warning("recurring rule %s failed: %s", rule.id, order.reject_reason)
            rule.last_run_at = now
            rule.next_run_at = advance_rule(rule, now)
            processed += 1
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
