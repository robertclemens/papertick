"""Yearly tax summary: what would be reportable if this were real money.

Taxable-brokerage sales use the FIFO lot engine's short/long-term split;
dividends in taxable accounts are reported as dividend income (qualified
status is not determined — check the fund's documentation); IRA activity is
summarized separately (contribution designations by tax year, withdrawals by
calendar year).
"""

from decimal import Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    Conversion,
    Dividend,
    OptionTransaction,
    OrderSide,
    Transaction,
    User,
)
from app.schemas import TaxYearSummaryOut
from app.services import ira

ZERO = Decimal("0")
CENT = Decimal("0.01")


def _dec(v) -> Decimal:
    return Decimal(v or 0).quantize(CENT)


def tax_report(db: Session, user: User, year: int, scenario_id: str | None,
               account_id: str | None = None) -> TaxYearSummaryOut:
    """One tax year, for one scenario.

    `scenario_id` is positional and required. It used to be an optional
    trailing keyword, and the router did not pass it: every figure here was
    then summed across *every* scenario the user owned — deleted ones
    included — so two tracks holding the same imported history reported double
    the contributions and double the rollovers. A scenario is a self-contained
    track of data, so a report that spans them is not a bigger report, it is a
    wrong one. Pass None only to deliberately aggregate a user with no
    scenarios at all.
    """
    q = select(Account).where(Account.user_id == user.id)
    if scenario_id:
        q = q.where(Account.scenario_id == scenario_id)
    if account_id:
        q = q.where(Account.id == account_id)
    accounts = list(db.execute(q).scalars())
    taxable_ids = [a.id for a in accounts if a.account_type == AccountType.TAXABLE]
    trad_ids = [a.id for a in accounts
                if a.account_type in (AccountType.TRADITIONAL_IRA, AccountType.ROLLOVER_IRA)]
    roth_ids = [a.id for a in accounts if a.account_type == AccountType.ROTH_IRA]
    ira_ids = trad_ids + roth_ids

    st = lt = unclassified = dividends = fees = ZERO
    if taxable_ids:
        rows = db.execute(
            select(
                func.coalesce(func.sum(Transaction.realized_st), 0),
                func.coalesce(func.sum(Transaction.realized_lt), 0),
                func.coalesce(func.sum(Transaction.fees), 0),
            )
            .where(
                Transaction.account_id.in_(taxable_ids),
                Transaction.side == OrderSide.SELL,
                extract("year", Transaction.as_of) == year,
            )
        ).one()
        st, lt, fees = _dec(rows[0]), _dec(rows[1]), _dec(rows[2])
        unclassified = _dec(db.execute(
            select(func.coalesce(func.sum(Transaction.realized_gains), 0))
            .where(
                Transaction.account_id.in_(taxable_ids),
                Transaction.side == OrderSide.SELL,
                Transaction.realized_st.is_(None),
                extract("year", Transaction.as_of) == year,
            )
        ).scalar_one())
        dividends = _dec(db.execute(
            select(func.coalesce(func.sum(Dividend.amount), 0))
            .where(
                Dividend.account_id.in_(taxable_ids),
                extract("year", Dividend.event_date) == year,
            )
        ).scalar_one())
        opt_rows = db.execute(
            select(
                func.coalesce(func.sum(OptionTransaction.realized_st), 0),
                func.coalesce(func.sum(OptionTransaction.realized_lt), 0),
                func.coalesce(func.sum(OptionTransaction.fees), 0),
            )
            .where(
                OptionTransaction.account_id.in_(taxable_ids),
                extract("year", OptionTransaction.as_of) == year,
            )
        ).one()
        st += _dec(opt_rows[0])
        lt += _dec(opt_rows[1])
        fees += _dec(opt_rows[2])

    def _flows(ids: list[str], kind: CashFlowKind, by_tax_year: bool) -> Decimal:
        if not ids:
            return ZERO
        q2 = select(func.coalesce(func.sum(Contribution.amount), 0)).where(
            Contribution.account_id.in_(ids), Contribution.kind == kind
        )
        if by_tax_year:
            q2 = q2.where(Contribution.tax_year == year)
        else:
            q2 = q2.where(extract("year", Contribution.timestamp) == year)
        return _dec(db.execute(q2).scalar_one())

    # Conversions out of the pre-tax side, and the part of them that was
    # ordinary income. Read off the Conversion rows rather than the cash-flow
    # pair, because only the Conversion row carries the pro-rata split.
    conv_gross = conv_taxable = ZERO
    if trad_ids:
        for c in db.execute(
            select(Conversion).where(Conversion.from_account_id.in_(trad_ids))
        ).scalars():
            if c.conversion_date.year == year:
                conv_gross += _dec(c.gross_amount)
                conv_taxable += _dec(c.taxable_amount)

    # After-tax basis still sitting on the pre-tax side — the Form 8606 running
    # figure that makes future distributions partly tax-free.
    basis_left = sum((_dec(a.after_tax_basis)
                      for a in accounts if a.id in trad_ids), ZERO)

    trad_withdrawals = ZERO - _flows(trad_ids, CashFlowKind.WITHDRAWAL, by_tax_year=False)
    roth_withdrawals = ZERO - _flows(roth_ids, CashFlowKind.WITHDRAWAL, by_tax_year=False)
    contributions = _flows(ira_ids, CashFlowKind.CONTRIBUTION, by_tax_year=True)
    rollovers = _flows(ira_ids, CashFlowKind.ROLLOVER, by_tax_year=False)
    # Money an account was opened with when a scenario was copied. Never a
    # rollover and never a contribution — reported on its own line so the cash
    # is visible without inflating either of the two figures the IRS cares
    # about.
    opening_balances = _flows([a.id for a in accounts],
                              CashFlowKind.OPENING_BALANCE, by_tax_year=False)

    # The 10% additional tax, recomputed from the withdrawals themselves so the
    # figure survives an import or a scenario copy — nothing about it is stored.
    penalty = ZERO
    if ira_ids:
        for c in db.execute(
            select(Contribution).where(Contribution.account_id.in_(ira_ids),
                                       Contribution.kind == CashFlowKind.WITHDRAWAL)
        ).scalars():
            if c.timestamp.year != year or c.penalty_exception:
                continue
            account = next((a for a in accounts if a.id == c.account_id), None)
            if account is None or ira.is_penalty_free_age(user, c.timestamp.date()):
                continue
            gross = -_dec(c.amount)
            if account.id in roth_ids:
                plan = ira.plan_roth_withdrawal(db, user, scenario_id, gross,
                                                c.timestamp.date())
            else:
                plan = ira.plan_traditional_withdrawal(db, user, scenario_id, gross,
                                                       c.timestamp.date())
            penalty += plan.penalty

    notes = [
        "Simulation only — not tax advice. Gains follow your elected cost-basis method (FIFO default).",
        "Dividend qualified/ordinary status is not determined; check each fund's documentation.",
        "Option results are included in the gains above; short option results are always short-term.",
    ]
    if unclassified:
        notes.append(
            "Some sales predate lot tracking and could not be split into short/long-term."
        )
    if trad_withdrawals > 0:
        notes.append("Traditional/Rollover IRA withdrawals would be ordinary income (plus a 10% penalty before 59½).")
    if conv_gross > 0:
        notes.append(
            f"Roth conversions of ${conv_gross} this year, of which ${conv_taxable} is "
            "ordinary income. Conversions have no annual limit and no income cap, so they "
            "use none of your contribution room — and each starts its own five-year clock."
        )
    if basis_left > 0:
        notes.append(
            f"${basis_left} of after-tax basis remains across your Traditional and Rollover "
            "IRAs (Form 8606). It comes out prorated against their combined value, never "
            "on its own."
        )
    notes.append(
        "Not modelled: required minimum distributions, the individual exceptions to the "
        "10% early-distribution penalty, state income tax, and the net investment income tax."
    )

    return TaxYearSummaryOut(
        year=year,
        account_id=account_id,
        short_term_gains=st,
        long_term_gains=lt,
        unclassified_gains=unclassified,
        dividends=dividends,
        fees=fees,
        traditional_withdrawals=trad_withdrawals,
        roth_withdrawals=roth_withdrawals,
        ira_contributions=contributions,
        rollovers=rollovers,
        opening_balances=opening_balances,
        conversions=conv_gross,
        conversion_taxable=conv_taxable,
        early_withdrawal_penalty=penalty,
        after_tax_basis=basis_left,
        notes=notes,
    )


def tax_report_csv(report: TaxYearSummaryOut) -> str:
    lines = [
        "item,amount_usd",
        f"tax_year,{report.year}",
        f"short_term_capital_gains,{report.short_term_gains}",
        f"long_term_capital_gains,{report.long_term_gains}",
        f"unclassified_gains,{report.unclassified_gains}",
        f"dividend_income,{report.dividends}",
        f"fees_paid,{report.fees}",
        f"traditional_ira_withdrawals,{report.traditional_withdrawals}",
        f"roth_ira_withdrawals,{report.roth_withdrawals}",
        f"ira_contributions_designated,{report.ira_contributions}",
        f"rollovers,{report.rollovers}",
        f"opening_balances,{report.opening_balances}",
        f"roth_conversions_gross,{report.conversions}",
        f"roth_conversions_taxable,{report.conversion_taxable}",
        f"early_withdrawal_penalty,{report.early_withdrawal_penalty}",
        f"after_tax_basis_remaining,{report.after_tax_basis}",
    ]
    return "\n".join(lines) + "\n"
