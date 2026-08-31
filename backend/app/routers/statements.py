from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, require_read
from app.models import Statement
from app.rate_limit import rate_limiter
from app.schemas import StatementOut
from app.services.statements import generate_missing

router = APIRouter(prefix="/statements", tags=["statements"])


@router.get("", response_model=list[StatementOut],
            dependencies=[Depends(rate_limiter("statements", 10, 60))])
def list_statements(principal: Principal = Depends(require_read),
                    db: Session = Depends(get_db)):
    """Archived statements for the active scenario, newest period first. Any
    completed month or year not yet rendered is generated on the fly before
    the list is returned."""
    # lazily backfill anything the monthly job hasn't produced yet
    generate_missing(db, principal.user, scenario_id=principal.scenario_id)
    rows = db.execute(
        select(Statement).where(Statement.user_id == principal.user.id,
                                Statement.scenario_id == principal.scenario_id)
        .order_by(Statement.period_start.desc())
    ).scalars().all()
    return [StatementOut.model_validate(s) for s in rows]


@router.get("/{statement_id}.pdf")
def download_statement(statement_id: str, principal: Principal = Depends(require_read),
                       db: Session = Depends(get_db)) -> Response:
    """The rendered PDF for one archived statement, served inline."""
    stmt = db.get(Statement, statement_id)
    if (stmt is None or stmt.user_id != principal.user.id
            or stmt.scenario_id != principal.scenario_id):
        raise HTTPException(status_code=404, detail="Statement not found")
    label = "year-end" if stmt.kind.value == "YEAR_END" else stmt.period_start.strftime("%Y-%m")
    return Response(
        content=stmt.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="papertick-{label}.pdf"'},
    )
