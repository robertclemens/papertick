from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, owned_account, require_read
from app.schemas import TaxYearSummaryOut
from app.services.tax import tax_report, tax_report_csv

router = APIRouter(prefix="/tax", tags=["tax"])


@router.get("/years")
def tax_years(principal: Principal = Depends(require_read),
              db: Session = Depends(get_db)) -> dict:
    """Years that actually have reportable activity (so the UI never offers an
    empty year). Always includes the current year."""
    from sqlalchemy import extract, select

    from app.models import (
        Account,
        Contribution,
        Dividend,
        OptionTransaction,
        Transaction,
    )

    ids = [a.id for a in db.execute(
        select(Account).where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
    ).scalars()]
    years: set[int] = {date.today().year}
    if ids:
        for model, column in (
            (Transaction, Transaction.as_of),
            (Dividend, Dividend.event_date),
            (Contribution, Contribution.timestamp),
            (OptionTransaction, OptionTransaction.as_of),
        ):
            rows = db.execute(
                select(extract("year", column))
                .where(model.account_id.in_(ids))
                .distinct()
            ).scalars()
            years.update(int(y) for y in rows if y)
    return {"years": sorted(years, reverse=True)}


@router.get("/report", response_model=TaxYearSummaryOut)
def get_tax_report(
    year: int | None = Query(default=None, ge=2015, le=2100),
    account_id: str | None = None,
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> TaxYearSummaryOut:
    """Reportable tax activity for one calendar year, scoped to the active
    scenario. Realized gains, dividends and fees cover taxable-brokerage
    accounts only — IRA activity is sheltered, never taxable, so it is
    summarized separately as contributions, rollovers and withdrawals.
    Defaults to the current year; narrow to one account with `account_id`."""
    if account_id:
        owned_account(account_id, principal, db)
    return tax_report(db, principal.user, year or date.today().year, account_id)


@router.get("/report.csv", response_class=PlainTextResponse)
def get_tax_report_csv(
    year: int | None = Query(default=None, ge=2015, le=2100),
    account_id: str | None = None,
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The same report as `/report`, as a downloadable CSV attachment."""
    if account_id:
        owned_account(account_id, principal, db)
    report = tax_report(db, principal.user, year or date.today().year, account_id)
    return PlainTextResponse(
        tax_report_csv(report),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="papertick-tax-{report.year}.csv"'},
    )
