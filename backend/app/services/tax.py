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
    Dividend,
    OptionTransaction,
    OrderSide,
    Transaction,
    User,
)
from app.schemas import TaxYearSummaryOut

ZERO = Decimal("0")
CENT = Decimal("0.01")


def _dec(v) -> Decimal:
    return Decimal(v or 0).quantize(CENT)


def tax_report(db: Session, user: User, year: int, account_id: str | None = None,
               scenario_id: str | None = None) -> TaxYearSummaryOut:
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

    trad_withdrawals = ZERO - _flows(trad_ids, CashFlowKind.WITHDRAWAL, by_tax_year=False)
    roth_withdrawals = ZERO - _flows(roth_ids, CashFlowKind.WITHDRAWAL, by_tax_year=False)
    contributions = _flows(ira_ids, CashFlowKind.CONTRIBUTION, by_tax_year=True)
    rollovers = _flows(ira_ids, CashFlowKind.ROLLOVER, by_tax_year=False)

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
    ]
    return "\n".join(lines) + "\n"
