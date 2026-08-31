from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, owned_account, require_read
from app.rate_limit import rate_limiter
from app.services import exports

router = APIRouter(prefix="/export", tags=["export"])

MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

TITLES = {
    "orders": "Orders",
    "transactions": "Transactions",
    "dividends": "Dividends",
}


@router.get("/{dataset}.{fmt}", dependencies=[Depends(rate_limiter("export", 20, 60))])
def export_history(
    dataset: str,
    fmt: str,
    range: str = Query(default="1y", pattern="^(1m|3m|6m|1y|3y|5y|10y|all)$"),
    account_id: str | None = None,
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> Response:
    """Download one history view for a timeframe as CSV or Excel. The window is
    the same one the History page shows, so an export matches the table."""
    if dataset not in exports.DATASETS:
        raise HTTPException(status_code=404, detail=f"Unknown export {dataset!r}")
    if fmt not in exports.FORMATS:
        raise HTTPException(status_code=404, detail=f"Unknown format {fmt!r}")
    if account_id:
        owned_account(account_id, principal, db)

    headers, rows, stem = exports.build(db, principal.user, dataset, range, account_id, principal.scenario_id)
    start = exports.window_start(range)
    period = f"{start.isoformat()} to {date.today().isoformat()}" if start else "all time"
    subtitle = f"PaperTick {TITLES[dataset]} — {period} ({len(rows)} rows)"

    if fmt == "csv":
        body = exports.to_csv(headers, rows)
    else:
        body = exports.to_xlsx(headers, rows, stem, subtitle)

    filename = f"papertick-{stem}-{range}-{date.today().isoformat()}.{fmt}"
    return Response(
        content=body,
        media_type=MEDIA[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
