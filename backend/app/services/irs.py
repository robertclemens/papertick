"""IRS rule engine for IRA contribution limits.

Rules implemented:
  - Annual IRA contribution limit shared across ALL of a user's Roth +
    Traditional (+ Rollover-typed) IRAs, per tax year.
  - Catch-up contribution for users who reach the catch-up age (50) by
    December 31 of the tax year.
  - Prior-year designation: between Jan 1 and Tax Day, a contribution may be
    designated to the previous tax year.
  - Rollovers are exempt from annual limits.
Income-based phase-outs are intentionally out of scope for this simulator.
"""

from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import for_update
from app.models import (
    IRA_TYPES,
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    IrsLimit,
    User,
)
from app.schemas import IrsStatusOut

CENT = Decimal("0.01")

# Accounts that share the one annual IRA contribution limit. A Rollover IRA is
# deliberately NOT one of them: mixing fresh contributions into rollover money
# commingles the account and forfeits the ability to roll it into a future
# employer plan, so this platform does not offer contributions there at all.
IRA_LIKE = set(IRA_TYPES)
CONTRIBUTABLE = set(IRA_TYPES)


def get_limit_row(db: Session, tax_year: int) -> IrsLimit:
    row = db.get(IrsLimit, tax_year)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"No IRS contribution limits configured for tax year {tax_year}",
        )
    return row


def allowed_tax_years(db: Session, today: date) -> list[int]:
    years = [today.year]
    prev = db.get(IrsLimit, today.year - 1)
    if prev is not None and today <= prev.designation_deadline:
        years.append(today.year - 1)
    return years


def user_limit(db: Session, user: User, tax_year: int) -> tuple[Decimal, bool]:
    row = get_limit_row(db, tax_year)
    age_at_year_end = tax_year - user.date_of_birth.year
    catchup = age_at_year_end >= row.catchup_age
    limit = Decimal(row.ira_limit) + (Decimal(row.ira_catchup) if catchup else Decimal("0"))
    return limit, catchup


def contributed_for_year(db: Session, user: User, tax_year: int,
                         scenario_id: str | None = None) -> Decimal:
    """The shared IRA limit is tracked per scenario: each track has its own
    contribution history, so a what-if cannot spend the real one's room."""
    q = (
        select(func.coalesce(func.sum(Contribution.amount), 0))
        .join(Account, Account.id == Contribution.account_id)
        .where(
            Account.user_id == user.id,
            Account.account_type.in_(IRA_LIKE),
            Contribution.tax_year == tax_year,
            Contribution.kind == CashFlowKind.CONTRIBUTION,
        )
    )
    if scenario_id:
        q = q.where(Account.scenario_id == scenario_id)
    return Decimal(db.execute(q).scalar_one())


def lock_contribution_scope(db: Session, user: User, scenario_id: str | None) -> None:
    """Serialise the shared annual IRA limit for one user (and scenario).

    The limit is shared across all of a user's IRAs, so locking only the account
    being deposited into is not enough: two deposits into two *different* IRAs
    both read "room available" and both commit. Locking the whole set makes the
    check-then-insert a single atomic step.

    Rows are locked in a deterministic order (by id) so two callers can never
    take the same pair in opposite orders and deadlock.
    """
    q = (
        select(Account)
        .where(Account.user_id == user.id, Account.account_type.in_(IRA_LIKE))
        .order_by(Account.id)
    )
    if scenario_id:
        q = q.where(Account.scenario_id == scenario_id)
    db.execute(for_update(q)).scalars().all()


def contributed_by_account(db: Session, account_id: str, tax_year: int) -> Decimal:
    """One account's share of the shared annual IRA limit."""
    total = db.execute(
        select(func.coalesce(func.sum(Contribution.amount), 0))
        .where(
            Contribution.account_id == account_id,
            Contribution.tax_year == tax_year,
            Contribution.kind == CashFlowKind.CONTRIBUTION,
        )
    ).scalar_one()
    return Decimal(total)


def _status_for(db: Session, account: Account, user: User, year: int, today: date):
    """One tax year's bucket for one account, or None when the year has no
    configured limit."""
    from app.schemas import ContributionStatusOut

    try:
        limit, catchup = user_limit(db, user, year)
    except HTTPException:
        return None
    row = get_limit_row(db, year)
    contributed = contributed_for_year(db, user, year, account.scenario_id)
    return ContributionStatusOut(
        tax_year=year,
        limit=limit,
        contributed=contributed,
        contributed_here=contributed_by_account(db, account.id, year),
        remaining=max(Decimal("0"), limit - contributed),
        used_pct=float(min(Decimal("100"), contributed / limit * 100)) if limit else 0.0,
        catchup_included=catchup,
        is_prior_year=year < today.year,
        designation_deadline=row.designation_deadline,
    )


def contribution_statuses(db: Session, account: Account, today: date | None = None) -> list:
    """Every contribution bucket this account can still be funded into, current
    year first. The prior year is included only while it is still open (on or
    before its designation deadline) AND has room left — an exhausted or closed
    year is not a bucket the user can act on, so it is not shown.

    Empty for taxable accounts (no limit) and for Rollover IRAs (no
    contributions accepted)."""
    if account.account_type not in CONTRIBUTABLE:
        return []
    today = today or date.today()
    user = db.get(User, account.user_id)
    if user is None:
        return []
    out = []
    current = _status_for(db, account, user, today.year, today)
    if current is not None:
        out.append(current)
    prev_row = db.get(IrsLimit, today.year - 1)
    if prev_row is not None and today <= prev_row.designation_deadline:
        prior = _status_for(db, account, user, today.year - 1, today)
        if prior is not None and prior.remaining > 0:
            out.append(prior)
    return out


def open_tax_years(db: Session, user: User, today: date | None = None,
                   scenario_id: str | None = None) -> list:
    """Buckets a deposit may be designated to, best-default first: the prior
    year while it is open and has room (using it up before it lapses is the
    default a brokerage offers), then the current year."""
    today = today or date.today()
    years = []
    prev_row = db.get(IrsLimit, today.year - 1)
    if prev_row is not None and today <= prev_row.designation_deadline:
        try:
            limit, _ = user_limit(db, user, today.year - 1)
        except HTTPException:
            limit = Decimal("0")
        remaining = max(Decimal("0"),
                        limit - contributed_for_year(db, user, today.year - 1, scenario_id))
        if remaining > 0:
            years.append((today.year - 1, remaining, prev_row.designation_deadline))
    try:
        limit, _ = user_limit(db, user, today.year)
        row = get_limit_row(db, today.year)
        remaining = max(Decimal("0"),
                        limit - contributed_for_year(db, user, today.year, scenario_id))
        years.append((today.year, remaining, row.designation_deadline))
    except HTTPException:
        pass
    return years


def default_tax_year(db: Session, user: User, today: date | None = None,
                     scenario_id: str | None = None) -> int:
    """Which bucket a new IRA contribution lands in unless the user picks
    another: the prior year while it is open with room left, else this year."""
    today = today or date.today()
    years = open_tax_years(db, user, today, scenario_id)
    return years[0][0] if years else today.year


def max_funding_plan(db: Session, account: Account, cadence, day_of_week: int | None,
                     day_of_month: int | None, month_of_year: int | None,
                     now: datetime | None = None):
    """Split the contribution room still available this tax year evenly across
    the runs a schedule would fire before the year ends.

    Per-run amounts are rounded UP to the cent, which makes the earlier runs
    whole and leaves the *last* one short by the rounding remainder — the run
    that gets trimmed anyway, because `fund_to_limit` caps every run at the
    room actually left when it fires. The year therefore lands exactly on the
    limit rather than a cent or two under it."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.models import utcnow
    from app.schemas import MaxFundingPlanOut
    from app.services.scheduling import occurrences

    now = now or utcnow()
    today = now.date()
    notes: list[str] = []

    if account.account_type not in CONTRIBUTABLE:
        return MaxFundingPlanOut(
            tax_year=today.year, remaining=Decimal("0"), runs=0,
            per_run=Decimal("0"), final_run=Decimal("0"), total=Decimal("0"),
            eligible=False,
            notes=[
                "A Rollover IRA takes rollover money only, so there is no "
                "contribution limit to fill here."
                if account.account_type == AccountType.ROLLOVER_IRA
                else "This account has no annual contribution limit to fill."
            ],
        )

    user = db.get(User, account.user_id)
    limit, catchup = user_limit(db, user, today.year)
    contributed = contributed_for_year(db, user, today.year, account.scenario_id)
    remaining = max(Decimal("0"), limit - contributed)

    # the window closes with the calendar year: money added in January defaults
    # to the prior year's bucket while that is still open, which is a different
    # plan than this one
    year_end = _dt(today.year, 12, 31, 23, 59, 59, tzinfo=_tz.utc)
    runs = occurrences(cadence, day_of_week, day_of_month, month_of_year, now, year_end)
    count = len(runs)

    if remaining <= 0:
        notes.append(
            f"Your {today.year} IRA limit of ${limit} is already fully contributed."
        )
    if count == 0:
        notes.append(
            f"This schedule has no runs left in {today.year}, so there is nothing to "
            "spread the remaining room across."
        )
    if remaining <= 0 or count == 0:
        return MaxFundingPlanOut(
            tax_year=today.year, remaining=remaining, runs=count,
            per_run=Decimal("0"), final_run=Decimal("0"), total=Decimal("0"),
            first_run=runs[0] if runs else None, last_run=runs[-1] if runs else None,
            catchup_included=catchup, eligible=False, notes=notes,
        )

    per_run = (remaining / count).quantize(CENT, ROUND_CEILING)
    # rounding up can make the ceiling cover the room in fewer runs than the
    # schedule has; drop the runs that would have nothing left to fund
    while count > 1 and per_run * (count - 1) >= remaining:
        count -= 1
    final_run = remaining - per_run * (count - 1)

    notes.append(
        f"${per_run} on each of {count} run{'s' if count != 1 else ''} through "
        f"{runs[count - 1].date().isoformat()}"
        + (f", with the last trimmed to ${final_run}" if final_run != per_run else "")
        + f" — ${remaining} in total."
    )
    if catchup:
        notes.append("Your limit includes the age-50+ catch-up amount.")
    return MaxFundingPlanOut(
        tax_year=today.year, remaining=remaining, runs=count,
        per_run=per_run, final_run=final_run, total=remaining,
        first_run=runs[0], last_run=runs[count - 1],
        catchup_included=catchup, eligible=True, notes=notes,
    )


def irs_status(db: Session, user: User, tax_year: int,
               scenario_id: str | None = None) -> IrsStatusOut:
    limit, catchup = user_limit(db, user, tax_year)
    contributed = contributed_for_year(db, user, tax_year, scenario_id)
    row = db.get(IrsLimit, tax_year)
    return IrsStatusOut(
        tax_year=tax_year,
        limit=limit,
        catchup_included=catchup,
        contributed=contributed,
        remaining=max(Decimal("0"), limit - contributed),
        source=getattr(row, "source", None) or "official",
    )


# ---------------------------------------------------------------- auto-maintenance

def tax_day(year: int) -> date:
    """Tax Day: April 15, rolled to Monday when it lands on a weekend."""
    d = date(year, 4, 15)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def ensure_limits(db: Session, today: date | None = None) -> list[int]:
    """Keep limits present for the current and next year. Missing years are
    carried forward from the latest known year as 'projected' (the IRS indexes
    IRA limits to inflation in $500 steps; projections should be replaced by
    the official figures via the seed table when published). Returns the years
    created."""
    today = today or date.today()
    created: list[int] = []
    for year in (today.year, today.year + 1):
        if db.get(IrsLimit, year) is not None:
            continue
        latest = db.execute(
            select(IrsLimit).where(IrsLimit.tax_year < year).order_by(IrsLimit.tax_year.desc())
        ).scalars().first()
        if latest is None:
            continue
        db.add(IrsLimit(
            tax_year=year,
            ira_limit=Decimal(latest.ira_limit),
            ira_catchup=Decimal(latest.ira_catchup),
            catchup_age=latest.catchup_age,
            designation_deadline=tax_day(year + 1),
            source="projected",
        ))
        created.append(year)
    if created:
        db.commit()
    return created


def validate_deposit(
    db: Session,
    user: User,
    account: Account,
    amount: Decimal,
    tax_year: int | None,
    kind: CashFlowKind,
    today: date | None = None,
) -> tuple[int | None, list[str], IrsStatusOut | None]:
    """Returns (resolved_tax_year, warnings, irs_status). Raises 422 on violations."""
    today = today or date.today()
    warnings: list[str] = []

    # An opening balance is written by a scenario copy, never deposited: it is
    # the value an account was carried across with, and accepting one here
    # would let external money enter an IRA outside the annual limit simply by
    # naming a different kind.
    if kind == CashFlowKind.OPENING_BALANCE:
        raise HTTPException(
            status_code=422,
            detail=("An opening balance is recorded when a scenario is copied, not "
                    "deposited. Use CONTRIBUTION, or ROLLOVER for money leaving a "
                    "retirement plan."),
        )

    if account.account_type == AccountType.TAXABLE:
        if tax_year is not None:
            raise HTTPException(
                status_code=422,
                detail="tax_year applies only to IRA accounts",
            )
        # Checked before the taxable early-return, not after it: a rollover is
        # money leaving a tax-advantaged plan for another one. There is no such
        # event for a brokerage account, and letting the kind through unchecked
        # is what put a six-figure "rollover" on a taxable account and then
        # reported it as rollover income on the tax summary.
        if kind == CashFlowKind.ROLLOVER:
            raise HTTPException(
                status_code=422,
                detail=("A taxable brokerage account cannot receive a rollover — a "
                        "rollover moves money between tax-advantaged accounts. Record "
                        "this as a deposit instead."),
            )
        return None, warnings, None

    if kind == CashFlowKind.ROLLOVER:
        if account.account_type == AccountType.ROTH_IRA:
            # Legal, but only from Roth-side money (a Roth 401(k)/403(b), or
            # another Roth IRA). Rolling *pre-tax* money in is a conversion and
            # is taxable, so it must not arrive on this path where nothing is
            # withheld and no Conversion row starts a five-year clock.
            warnings.append(
                "Rollover into a Roth IRA recorded. This is only tax-free from Roth "
                "money — a Roth 401(k)/403(b) or another Roth IRA. Pre-tax money "
                "moved into a Roth is a conversion: it is ordinary income and starts "
                "its own five-year clock, so record it with a Roth conversion instead."
            )
        warnings.append("Rollover recorded: rollovers do not count toward annual IRA limits.")
        return None, warnings, None

    if account.account_type == AccountType.ROLLOVER_IRA:
        raise HTTPException(
            status_code=422,
            detail=(
                "A Rollover IRA holds rollover money only. Adding a regular "
                "contribution commingles the account, which forfeits the option to "
                "roll it into a future employer plan — deposit it as a rollover, or "
                "contribute to your Roth or Traditional IRA instead."
            ),
        )

    # IRA contribution path. With no year named, money lands in the bucket a
    # brokerage would default to: the prior year while it is still open and has
    # room (it lapses at Tax Day), otherwise the current year.
    resolved_year = (tax_year if tax_year is not None
                     else default_tax_year(db, user, today, account.scenario_id))
    allowed = allowed_tax_years(db, today)
    if resolved_year not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Contributions can only be designated to tax year(s) {allowed} right now. "
                "Prior-year designation is allowed only between Jan 1 and Tax Day."
            ),
        )

    status = irs_status(db, user, resolved_year, account.scenario_id)
    if amount > status.remaining:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Contribution of ${amount} exceeds the {resolved_year} IRA limit of "
                f"${status.limit} (already contributed ${status.contributed}, "
                f"remaining ${status.remaining}). The limit is shared across all of "
                "your Roth and Traditional IRAs."
            ),
        )

    remaining_after = status.remaining - amount
    if remaining_after <= status.limit * Decimal("0.1"):
        warnings.append(
            f"After this contribution you will have ${remaining_after} of your "
            f"{resolved_year} IRA limit remaining."
        )
    return resolved_year, warnings, irs_status_after(status, amount)


def irs_status_after(status: IrsStatusOut, amount: Decimal) -> IrsStatusOut:
    return IrsStatusOut(
        tax_year=status.tax_year,
        limit=status.limit,
        catchup_included=status.catchup_included,
        contributed=status.contributed + amount,
        remaining=max(Decimal("0"), status.remaining - amount),
    )
