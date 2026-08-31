from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, owned_account, require_read, require_trade
from app.models import Account, Cadence, RecurringRule, RuleStatus, utcnow
from app.schemas import (
    MaxFundingIn,
    MaxFundingPlanOut,
    ScheduleCreateIn,
    ScheduleOut,
    ScheduleUpdateIn,
)
from app.services import irs
from app.services.scheduling import compute_next_run
from app.services.trading import require_asset

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _owned_rule(rule_id: str, principal: Principal, db: Session) -> RecurringRule:
    rule = db.get(RecurringRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    owned_account(rule.account_id, principal, db)
    return rule


@router.get("", response_model=list[ScheduleOut])
def list_schedules(principal: Principal = Depends(require_read), db: Session = Depends(get_db)):
    """Recurring investment rules across the active scenario, newest first."""
    rows = db.execute(
        select(RecurringRule)
        .join(Account, Account.id == RecurringRule.account_id)
        .where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
        .order_by(RecurringRule.created_at.desc())
    ).scalars().all()
    return [ScheduleOut.model_validate(r) for r in rows]


@router.post("/max-funding", response_model=MaxFundingPlanOut)
def max_funding(data: MaxFundingIn, principal: Principal = Depends(require_read),
                db: Session = Depends(get_db)) -> MaxFundingPlanOut:
    """What per-run amount fills this year's remaining IRA contribution room
    across the runs this schedule has left."""
    account = owned_account(data.account_id, principal, db)
    return irs.max_funding_plan(
        db, account, data.cadence, data.day_of_week, data.day_of_month, data.month_of_year
    )


@router.post("", response_model=ScheduleOut, status_code=201)
def create_schedule(data: ScheduleCreateIn, principal: Principal = Depends(require_trade),
                    db: Session = Depends(get_db)) -> ScheduleOut:
    """Start a recurring investment. The first run lands at the next matching
    NYSE market open; with `fund_to_limit` set, each run is capped at whatever
    IRA contribution room remains when it actually fires, not when it is created."""
    account = owned_account(data.account_id, principal, db)
    require_asset(db, data.ticker)
    now = utcnow()
    month_of_year = data.month_of_year
    if data.cadence in (Cadence.QUARTERLY, Cadence.ANNUALLY) and month_of_year is None:
        month_of_year = now.astimezone().month  # anchor on the current month
    rule = RecurringRule(
        account_id=account.id,
        ticker=data.ticker,
        amount=data.amount,
        cadence=data.cadence,
        day_of_week=data.day_of_week,
        day_of_month=data.day_of_month,
        month_of_year=month_of_year,
        fund_to_limit=data.fund_to_limit,
        next_run_at=compute_next_run(
            data.cadence, data.day_of_week, data.day_of_month, now, month_of_year
        ),
    )
    db.add(rule)
    db.commit()
    return ScheduleOut.model_validate(rule)


@router.patch("/{rule_id}", response_model=ScheduleOut)
def update_schedule(rule_id: str, data: ScheduleUpdateIn,
                    principal: Principal = Depends(require_trade),
                    db: Session = Depends(get_db)) -> ScheduleOut:
    """Edit a recurring investment. Only future runs are affected — trades
    already executed by this rule are left exactly as they were."""
    rule = _owned_rule(rule_id, principal, db)
    if rule.status == RuleStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="Schedule is cancelled")

    if data.ticker is not None and data.ticker != rule.ticker:
        require_asset(db, data.ticker)
        rule.ticker = data.ticker
    if data.amount is not None:
        rule.amount = data.amount
    if data.fund_to_limit is not None:
        rule.fund_to_limit = data.fund_to_limit

    cadence = data.cadence or rule.cadence
    dow = data.day_of_week if data.day_of_week is not None else rule.day_of_week
    dom = data.day_of_month if data.day_of_month is not None else rule.day_of_month
    moy = data.month_of_year if data.month_of_year is not None else rule.month_of_year
    if cadence in (Cadence.MONTHLY, Cadence.QUARTERLY, Cadence.ANNUALLY):
        dow = None
        dom = dom or 1
        if cadence == Cadence.MONTHLY:
            moy = None
        elif moy is None:
            moy = utcnow().astimezone().month
    elif cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY):
        dom = moy = None
        dow = dow if dow is not None else 0
    else:  # DAILY
        dow = dom = moy = None

    timing_changed = (
        cadence != rule.cadence or dow != rule.day_of_week
        or dom != rule.day_of_month or moy != rule.month_of_year
    )
    rule.cadence = cadence
    rule.day_of_week = dow
    rule.day_of_month = dom
    rule.month_of_year = moy
    if timing_changed and rule.status == RuleStatus.ACTIVE:
        rule.next_run_at = compute_next_run(cadence, dow, dom, utcnow(), moy)
    db.commit()
    return ScheduleOut.model_validate(rule)


@router.post("/{rule_id}/pause", response_model=ScheduleOut)
def pause_schedule(rule_id: str, principal: Principal = Depends(require_trade),
                   db: Session = Depends(get_db)) -> ScheduleOut:
    """Suspend a recurring investment so no further runs fire. Already
    executed trades are untouched, and it can be reactivated with `/resume`."""
    rule = _owned_rule(rule_id, principal, db)
    if rule.status == RuleStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="Schedule is cancelled")
    rule.status = RuleStatus.PAUSED
    db.commit()
    return ScheduleOut.model_validate(rule)


@router.post("/{rule_id}/resume", response_model=ScheduleOut)
def resume_schedule(rule_id: str, principal: Principal = Depends(require_trade),
                    db: Session = Depends(get_db)) -> ScheduleOut:
    """Reactivate a paused schedule. The next run is recomputed from now, so
    runs missed while paused are not made up."""
    rule = _owned_rule(rule_id, principal, db)
    if rule.status == RuleStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="Schedule is cancelled")
    rule.status = RuleStatus.ACTIVE
    rule.next_run_at = compute_next_run(
        rule.cadence, rule.day_of_week, rule.day_of_month, utcnow(), rule.month_of_year
    )
    db.commit()
    return ScheduleOut.model_validate(rule)


@router.delete("/{rule_id}", response_model=ScheduleOut)
def cancel_schedule(rule_id: str, principal: Principal = Depends(require_trade),
                    db: Session = Depends(get_db)) -> ScheduleOut:
    """Cancel a recurring investment for good. Unlike pausing, a cancelled
    schedule is terminal and cannot be resumed."""
    rule = _owned_rule(rule_id, principal, db)
    rule.status = RuleStatus.CANCELLED
    db.commit()
    return ScheduleOut.model_validate(rule)
