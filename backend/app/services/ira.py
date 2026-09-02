"""IRA tax mechanics: Form 8606 pro-rata, Roth withdrawal ordering, the two
five-year clocks, and the 10% early-distribution penalty.

The rules modelled here are the ones that change what a number *is*, not merely
what it is called:

  * **Pro-rata (Form 8606).** Money leaving a Traditional/Rollover IRA carries
    after-tax basis in proportion, computed across *every* such IRA together —
    you cannot isolate the after-tax dollars by putting them in their own
    account. This is the rule that makes a backdoor Roth work, or not.
  * **Roth ordering.** A non-qualified Roth distribution comes out in a fixed
    order — regular contributions, then conversions oldest-first, then earnings
    — and each layer is taxed and penalised differently.
  * **Two five-year clocks.** One per person, for whether *earnings* come out
    tax-free; one per conversion, for whether converted money escapes the 10%
    penalty. They are different clocks answering different questions, and
    conflating them is the classic way to get this wrong.

Deliberately **not** modelled, and stated rather than implied wherever these
figures are shown: required minimum distributions, the individual exceptions to
the 10% penalty (recorded as an attestation instead — each needs facts the
platform cannot see), state income tax, and the net investment income tax.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    Conversion,
    Position,
    User,
)
from app.services.market_data import MarketDataError, market_data

ZERO = Decimal("0")
CENT = Decimal("0.01")

PENALTY_RATE = Decimal("0.10")
#: Age at which the 10% early-distribution penalty stops applying.
PENALTY_FREE_AGE_DAYS = 59.5 * 365.25
#: Both clocks run for five tax years, counted from January 1 of the start year.
FIVE_YEARS = 5

#: Accounts whose balances are aggregated for the pro-rata rule. A Rollover IRA
#: is a Traditional IRA for this purpose; the distinction only matters for
#: keeping employer-plan money portable.
PRE_TAX_TYPES = (AccountType.TRADITIONAL_IRA, AccountType.ROLLOVER_IRA)


def _accounts(db: Session, user: User, scenario_id: str | None,
              types: tuple[AccountType, ...]) -> list[Account]:
    q = select(Account).where(Account.user_id == user.id,
                              Account.account_type.in_(types))
    if scenario_id:
        q = q.where(Account.scenario_id == scenario_id)
    return list(db.execute(q).scalars())


def pre_tax_accounts(db: Session, user: User, scenario_id: str | None) -> list[Account]:
    """Every Traditional and Rollover IRA the pro-rata rule aggregates over."""
    return _accounts(db, user, scenario_id, PRE_TAX_TYPES)


def roth_accounts(db: Session, user: User, scenario_id: str | None) -> list[Account]:
    return _accounts(db, user, scenario_id, (AccountType.ROTH_IRA,))


def account_value(db: Session, account: Account) -> Decimal:
    """Settlement cash plus the market value of everything held."""
    total = Decimal(account.settlement_balance)
    for pos in db.execute(
        select(Position).where(Position.account_id == account.id)
    ).scalars():
        shares = Decimal(pos.shares)
        if not shares:
            continue
        try:
            price = market_data.quote(pos.ticker).price
        except MarketDataError:
            price = Decimal(pos.average_cost)
        total += shares * price
    return total


def age_on(user: User, on: date) -> float:
    return (on - user.date_of_birth).days / 365.25


def is_penalty_free_age(user: User, on: date) -> bool:
    return (on - user.date_of_birth).days >= PENALTY_FREE_AGE_DAYS


# --------------------------------------------------------------- pro-rata


@dataclass
class ProRata:
    """How a distribution from the pre-tax side splits, per Form 8606."""

    taxable: Decimal
    nontaxable: Decimal
    #: combined value of every Traditional/Rollover IRA before the distribution
    total_value: Decimal
    #: combined after-tax basis across those same accounts
    total_basis: Decimal

    @property
    def basis_fraction(self) -> Decimal:
        if self.total_value <= ZERO:
            return ZERO
        return self.total_basis / self.total_value


def pro_rata(db: Session, user: User, scenario_id: str | None,
             amount: Decimal) -> ProRata:
    """Split a Traditional/Rollover IRA distribution into taxable and not.

    Aggregated across every pre-tax IRA, which is the whole point of the rule:
    a nondeductible contribution parked in its own account does not come out
    tax-free, it dilutes into the combined basis. Callers must therefore pass
    the *scenario*, never a single account — and a UI filter on one account may
    change what is displayed but must never change what is computed.
    """
    accounts = pre_tax_accounts(db, user, scenario_id)
    total_value = sum((account_value(db, a) for a in accounts), ZERO)
    total_basis = sum((Decimal(a.after_tax_basis or 0) for a in accounts), ZERO)

    if total_value <= ZERO or total_basis <= ZERO:
        return ProRata(taxable=amount, nontaxable=ZERO,
                       total_value=total_value, total_basis=total_basis)

    fraction = min(total_basis / total_value, Decimal("1"))
    nontaxable = (amount * fraction).quantize(CENT)
    if nontaxable > total_basis:
        nontaxable = total_basis.quantize(CENT)
    return ProRata(taxable=(amount - nontaxable).quantize(CENT), nontaxable=nontaxable,
                   total_value=total_value, total_basis=total_basis)


def consume_basis(db: Session, user: User, scenario_id: str | None,
                  nontaxable: Decimal) -> None:
    """Draw `nontaxable` out of the combined after-tax basis, proportionally.

    Spread across the pre-tax accounts in proportion to the basis each holds,
    because the rule that produced the figure aggregated them the same way.
    """
    if nontaxable <= ZERO:
        return
    accounts = [a for a in pre_tax_accounts(db, user, scenario_id)
                if Decimal(a.after_tax_basis or 0) > ZERO]
    total = sum((Decimal(a.after_tax_basis) for a in accounts), ZERO)
    if total <= ZERO:
        return
    remaining = nontaxable
    for i, a in enumerate(accounts):
        basis = Decimal(a.after_tax_basis)
        # the last account absorbs the rounding remainder so the books close
        take = remaining if i == len(accounts) - 1 else (nontaxable * basis / total).quantize(CENT)
        take = min(take, basis, remaining)
        a.after_tax_basis = (basis - take).quantize(CENT)
        remaining -= take
        if remaining <= ZERO:
            break


# ------------------------------------------------------------- five-year clocks


def roth_clock_start_year(db: Session, user: User, scenario_id: str | None) -> int | None:
    """The tax year that started this person's Roth five-year clock.

    January 1 of the year of their *first* Roth contribution or conversion. One
    clock per person, not per account, and it never restarts — closing a Roth
    and opening another does not reset it.
    """
    years: list[int] = []
    roth_ids = [a.id for a in roth_accounts(db, user, scenario_id)]
    if roth_ids:
        for (ts,) in db.execute(
            select(Contribution.timestamp).where(
                Contribution.account_id.in_(roth_ids),
                Contribution.kind.in_((CashFlowKind.CONTRIBUTION,
                                       CashFlowKind.ROLLOVER,
                                       CashFlowKind.CONVERSION)),
                Contribution.amount > 0,
            )
        ):
            years.append(ts.year)
        for (d,) in db.execute(
            select(Conversion.conversion_date).where(Conversion.to_account_id.in_(roth_ids))
        ):
            years.append(d.year)
    return min(years) if years else None


def roth_five_year_met(db: Session, user: User, scenario_id: str | None,
                       on: date) -> bool:
    start = roth_clock_start_year(db, user, scenario_id)
    return start is not None and on.year - start >= FIVE_YEARS


def roth_is_qualified(db: Session, user: User, scenario_id: str | None,
                      on: date) -> bool:
    """Whether Roth *earnings* come out tax-free.

    Needs both halves: the five-year clock done, and a qualifying event — here,
    reaching 59½. Death and disability also qualify in reality; neither is
    something this platform can observe.
    """
    return roth_five_year_met(db, user, scenario_id, on) and is_penalty_free_age(user, on)


def conversion_five_year_met(conversion: Conversion, on: date) -> bool:
    """Whether this particular conversion's own clock has run out.

    Separate from the account clock and asked for a different reason: this one
    decides whether converted money escapes the 10% penalty, not whether
    earnings are tax-free.
    """
    return on.year - conversion.conversion_date.year >= FIVE_YEARS


# ------------------------------------------------------- Roth withdrawal ordering


@dataclass
class WithdrawalLayer:
    label: str
    amount: Decimal
    taxable: Decimal
    penalty: Decimal


@dataclass
class WithdrawalPlan:
    """What a distribution would cost, layer by layer."""

    gross: Decimal
    layers: list[WithdrawalLayer] = field(default_factory=list)
    taxable_income: Decimal = ZERO
    penalty: Decimal = ZERO
    qualified: bool = False
    notes: list[str] = field(default_factory=list)
    #: conversions drawn on, with how much of each part was used — applied only
    #: when the withdrawal is actually executed
    conversion_draws: list[tuple[Conversion, Decimal, Decimal]] = field(default_factory=list)
    #: after-tax basis consumed from the pre-tax side (Traditional withdrawals)
    basis_used: Decimal = ZERO


def roth_contribution_pool(db: Session, user: User, scenario_id: str | None) -> Decimal:
    """Regular Roth contributions still inside the Roth.

    Contributions come out first and are always tax- and penalty-free, so this
    is the first layer of any non-qualified distribution. Rollover-kind deposits
    into a Roth are counted here too; conversions are not — they have their own
    layer with their own clocks.
    """
    roth_ids = [a.id for a in roth_accounts(db, user, scenario_id)]
    if not roth_ids:
        return ZERO
    total = ZERO
    for c in db.execute(
        select(Contribution).where(Contribution.account_id.in_(roth_ids))
    ).scalars():
        if c.kind in (CashFlowKind.CONTRIBUTION, CashFlowKind.ROLLOVER):
            total += Decimal(c.amount)
        elif c.kind == CashFlowKind.WITHDRAWAL:
            # withdrawals draw contributions down first, by the ordering rules
            total += Decimal(c.amount)   # already negative
    return max(total, ZERO)


def plan_roth_withdrawal(db: Session, user: User, scenario_id: str | None,
                         amount: Decimal, on: date,
                         penalty_exception: bool = False) -> WithdrawalPlan:
    """Apply the Roth ordering rules to a distribution.

    Contributions, then conversions oldest-first (taxable part of each before
    its nontaxable part), then earnings. A qualified distribution skips all of
    it and comes out clean.
    """
    plan = WithdrawalPlan(gross=amount)
    plan.qualified = roth_is_qualified(db, user, scenario_id, on)
    if plan.qualified:
        plan.layers.append(WithdrawalLayer("Qualified distribution", amount, ZERO, ZERO))
        plan.notes.append(
            "Qualified: the five-year clock is met and you are 59½ or older, so the "
            "whole distribution is tax-free and penalty-free."
        )
        return plan

    under_age = not is_penalty_free_age(user, on)
    remaining = amount

    pool = roth_contribution_pool(db, user, scenario_id)
    take = min(pool, remaining)
    if take > ZERO:
        plan.layers.append(WithdrawalLayer("Regular contributions", take, ZERO, ZERO))
        remaining -= take

    roth_ids = [a.id for a in roth_accounts(db, user, scenario_id)]
    conversions = list(db.execute(
        select(Conversion)
        .where(Conversion.to_account_id.in_(roth_ids))
        .order_by(Conversion.conversion_date, Conversion.created_at)
    ).scalars()) if roth_ids else []

    for conv in conversions:
        if remaining <= ZERO:
            break
        seasoned = conversion_five_year_met(conv, on)
        # the previously-taxable part comes out before the previously-untaxed
        # part of the same conversion, and only that part can be penalised
        taxable_part = min(Decimal(conv.taxable_remaining), remaining)
        used_taxable = ZERO
        used_nontaxable = ZERO
        if taxable_part > ZERO:
            penalty = ZERO
            if under_age and not seasoned and not penalty_exception:
                penalty = (taxable_part * PENALTY_RATE).quantize(CENT)
            plan.layers.append(WithdrawalLayer(
                f"Conversion of {conv.conversion_date.isoformat()} (taxed at conversion)",
                taxable_part, ZERO, penalty,
            ))
            plan.penalty += penalty
            remaining -= taxable_part
            used_taxable = taxable_part

        nontaxable_part = min(Decimal(conv.nontaxable_remaining), remaining)
        if nontaxable_part > ZERO:
            plan.layers.append(WithdrawalLayer(
                f"Conversion of {conv.conversion_date.isoformat()} (after-tax portion)",
                nontaxable_part, ZERO, ZERO,
            ))
            remaining -= nontaxable_part
            used_nontaxable = nontaxable_part

        if used_taxable or used_nontaxable:
            plan.conversion_draws.append((conv, used_taxable, used_nontaxable))
            if under_age and not seasoned and used_taxable:
                plan.notes.append(
                    f"The {conv.conversion_date.year} conversion is less than five years "
                    "old, so the converted amount drawn from it carries the 10% penalty "
                    "even though it was already taxed."
                )

    if remaining > ZERO:
        # anything past contributions and conversions is earnings
        penalty = ZERO
        if under_age and not penalty_exception:
            penalty = (remaining * PENALTY_RATE).quantize(CENT)
        plan.layers.append(WithdrawalLayer("Earnings", remaining, remaining, penalty))
        plan.taxable_income += remaining
        plan.penalty += penalty
        plan.notes.append(
            "Part of this comes from earnings, which are ordinary income in a "
            "non-qualified Roth distribution."
        )

    if penalty_exception and under_age:
        plan.notes.append(
            "A penalty exception was claimed, so the 10% additional tax is not applied. "
            "The individual exceptions are not verified here."
        )
    return plan


def plan_traditional_withdrawal(db: Session, user: User, scenario_id: str | None,
                                amount: Decimal, on: date,
                                penalty_exception: bool = False) -> WithdrawalPlan:
    """A Traditional/Rollover distribution: pro-rata taxable, 10% on that part."""
    plan = WithdrawalPlan(gross=amount)
    split = pro_rata(db, user, scenario_id, amount)
    under_age = not is_penalty_free_age(user, on)

    penalty = ZERO
    if under_age and not penalty_exception and split.taxable > ZERO:
        penalty = (split.taxable * PENALTY_RATE).quantize(CENT)

    plan.layers.append(WithdrawalLayer("Pre-tax (ordinary income)", split.taxable,
                                       split.taxable, penalty))
    if split.nontaxable > ZERO:
        plan.layers.append(WithdrawalLayer("After-tax basis (already taxed)",
                                           split.nontaxable, ZERO, ZERO))
        plan.notes.append(
            f"{split.basis_fraction * 100:.2f}% of every dollar leaving your Traditional "
            "and Rollover IRAs is after-tax basis, so that share comes out tax-free. The "
            "proportion is fixed across all of them together — Form 8606's pro-rata rule."
        )
    plan.taxable_income = split.taxable
    plan.penalty = penalty
    plan.basis_used = split.nontaxable
    if under_age and penalty > ZERO:
        plan.notes.append(
            "Before 59½, the taxable part of an IRA distribution also carries a 10% "
            "additional tax."
        )
    if penalty_exception and under_age:
        plan.notes.append(
            "A penalty exception was claimed, so the 10% additional tax is not applied. "
            "The individual exceptions are not verified here."
        )
    return plan
