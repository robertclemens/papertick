"""Next-run computation for recurring investment rules.

Recurring buys execute at the NYSE market open (9:30 AM ET, DST-correct) of
their target day; a target that lands on a weekend or holiday rolls forward to
the next trading day.
"""

from datetime import date, datetime, timedelta, timezone

from app.models import Cadence, RecurringRule
from app.services import market_calendar as cal


def _run_at(d: date) -> datetime:
    return cal.market_open_at(cal.next_trading_day(d))


MONTH_STEP = {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.ANNUALLY: 12}


def _add_months(d: date, months: int) -> date:
    total = (d.year * 12 + d.month - 1) + months
    return date(total // 12, total % 12 + 1, d.day)


def compute_next_run(
    cadence: Cadence,
    day_of_week: int | None,
    day_of_month: int | None,
    after: datetime,
    month_of_year: int | None = None,
) -> datetime:
    after = after.astimezone(timezone.utc)
    d = after.astimezone(cal.ET).date()

    if cadence == Cadence.DAILY:
        while _run_at(d) <= after:
            d += timedelta(days=1)
        return _run_at(d)

    if cadence in (Cadence.WEEKLY, Cadence.BIWEEKLY):
        dow = day_of_week if day_of_week is not None else 0
        while d.weekday() != dow or _run_at(d) <= after:
            d += timedelta(days=1)
        return _run_at(d)

    # MONTHLY / QUARTERLY / ANNUALLY
    step = MONTH_STEP[cadence]
    dom = min(day_of_month if day_of_month is not None else 1, 28)
    anchor = month_of_year if month_of_year is not None else d.month
    candidate = date(d.year, anchor, dom)
    # walk back to the first occurrence at or before now, then forward by step
    while candidate > d:
        candidate = _add_months(candidate, -step)
    while _run_at(candidate) <= after:
        candidate = _add_months(candidate, step)
    return _run_at(candidate)


def advance_rule(rule: RecurringRule, ran_at: datetime) -> datetime:
    if rule.cadence == Cadence.BIWEEKLY:
        return _run_at(ran_at.astimezone(cal.ET).date() + timedelta(days=14))
    return compute_next_run(
        rule.cadence, rule.day_of_week, rule.day_of_month, ran_at, rule.month_of_year
    )


def occurrences(cadence: Cadence, day_of_week: int | None, day_of_month: int | None,
                month_of_year: int | None, after: datetime, until: datetime,
                cap: int = 400) -> list[datetime]:
    """Every run this rule would fire strictly after `after` and on or before
    `until`. `cap` guards a runaway loop on a daily cadence over a long window."""
    out: list[datetime] = []
    cursor = after
    while len(out) < cap:
        if cadence == Cadence.BIWEEKLY and out:
            nxt = _run_at(out[-1].astimezone(cal.ET).date() + timedelta(days=14))
        else:
            nxt = compute_next_run(cadence, day_of_week, day_of_month, cursor, month_of_year)
        if nxt > until:
            break
        out.append(nxt)
        cursor = nxt
    return out
