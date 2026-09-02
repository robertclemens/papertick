import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, require_manage, require_read
from app.models import Account, Scenario, Transaction, utcnow
from app.rate_limit import rate_limiter
from app.schemas import (
    DeletedScenarioOut,
    PurgeResultOut,
    ScenarioCreateIn,
    ScenarioImportIn,
    ScenarioOut,
    ScenarioUpdateIn,
)
from app.services import scenarios as svc

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def _out(db: Session, principal: Principal, scenario: Scenario) -> ScenarioOut:
    accounts = db.query(Account).filter(Account.scenario_id == scenario.id).count()
    # counted rather than inferred from the toggle: turning past-dated fills
    # back off does not remove the ones already in the book, and it is the
    # fills, not the setting, that a reader of these numbers needs to know about
    backdated = (
        db.query(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .filter(Account.scenario_id == scenario.id, Transaction.backdated.is_(True))
        .count()
    )
    return ScenarioOut(
        id=scenario.id,
        name=scenario.name,
        description=scenario.description,
        sort_order=scenario.sort_order,
        copied_from_id=scenario.copied_from_id,
        account_count=accounts,
        is_default=principal.user.default_scenario_id == scenario.id,
        is_active=principal.scenario_id == scenario.id,
        allow_backdated=scenario.allow_backdated,
        backdated_fills=backdated,
        created_at=scenario.created_at,
    )


@router.get("", response_model=list[ScenarioOut])
def list_scenarios(principal: Principal = Depends(require_read),
                   db: Session = Depends(get_db)):
    """Every scenario the user has, with its account count and which one is
    the default and which one is active for this request."""
    return [_out(db, principal, s) for s in svc.list_for(db, principal.user)]


@router.post("", response_model=ScenarioOut, status_code=201)
def create_scenario(data: ScenarioCreateIn, principal: Principal = Depends(require_manage),
                    db: Session = Depends(get_db)) -> ScenarioOut:
    """Start a new track.

    With `copy_from_id`, `copy_mode` decides how much comes across:
    `position` (the default) brings only the balances and holdings, re-priced
    at today's market, so the new scenario starts flat and measures itself from
    day one; `full` duplicates the source exactly — orders, transactions, tax
    lots, dividends, contributions and auto-invest rules — so returns and
    history carry over."""
    scenario = svc.create(db, principal.user, data.name, data.description,
                          data.copy_from_id, data.copy_mode)
    db.commit()
    return _out(db, principal, scenario)


@router.post("/import", response_model=ScenarioOut, status_code=201,
             dependencies=[Depends(rate_limiter("scenario-import", 10, 3600))])
def import_scenario(data: ScenarioImportIn, principal: Principal = Depends(require_manage),
                    db: Session = Depends(get_db)) -> ScenarioOut:
    """Restore an export. With `target_scenario_id` the named scenario is
    emptied and replaced in place; without it a new scenario is created."""
    scenario = svc.import_scenario(
        db, principal.user, data.payload, data.target_scenario_id, data.name
    )
    db.commit()
    return _out(db, principal, scenario)


@router.patch("/{scenario_id}", response_model=ScenarioOut)
def update_scenario(scenario_id: str, data: ScenarioUpdateIn,
                    principal: Principal = Depends(require_manage),
                    db: Session = Depends(get_db)) -> ScenarioOut:
    """Rename, redescribe, set a scenario as the default, or turn past-dated
    fills on or off for it. Setting `is_default` does not switch this request's
    active scenario — that is chosen separately via the `X-Scenario-Id` header.

    `allow_backdated` is opt-in per scenario because a past-dated order is
    placed knowing the outcome. Turning it back off stops new ones; it does not
    un-mark the fills already made, and it never will."""
    scenario = svc.owned(db, principal.user, scenario_id)
    if data.name is not None:
        scenario.name = svc._unique_name(db, principal.user, data.name.strip(),
                                         exclude_id=scenario.id)
    if data.description is not None:
        scenario.description = data.description or None
    if data.is_default:
        principal.user.default_scenario_id = scenario.id
    if data.allow_backdated is not None:
        scenario.allow_backdated = data.allow_backdated
    db.commit()
    return _out(db, principal, scenario)


@router.get("/deleted", response_model=list[DeletedScenarioOut])
def list_deleted(principal: Principal = Depends(require_read),
                 db: Session = Depends(get_db)):
    """Scenarios in the retention window, with how long each has left."""
    now = utcnow()
    out = []
    for s in svc.list_deleted(db, principal.user):
        due = svc.purges_at(s)
        remaining = (due - now).total_seconds() if due else 0
        out.append(DeletedScenarioOut(
            id=s.id,
            name=s.name,
            description=s.description,
            account_count=db.query(Account).filter(Account.scenario_id == s.id).count(),
            deleted_at=s.deleted_at,
            purges_at=due,
            days_left=max(0, int(remaining // 86400)),
            hours_left=max(0, int(remaining // 3600)),
            retention_days=svc.retention_days(),
        ))
    return out


@router.delete("/deleted/purge", response_model=PurgeResultOut)
def purge_all_scenarios(principal: Principal = Depends(require_manage),
                        db: Session = Depends(get_db)) -> PurgeResultOut:
    """Destroy every deleted scenario now. This cannot be undone."""
    purged = svc.purge_all(db, principal.user)
    db.commit()
    return PurgeResultOut(purged=purged)


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: str, principal: Principal = Depends(require_manage),
                    db: Session = Depends(get_db)) -> None:
    """Move a scenario to the deleted list. Its data is kept for the retention
    window so this is recoverable; `/purge` is what destroys it."""
    scenario = svc.owned(db, principal.user, scenario_id)
    svc.delete(db, principal.user, scenario)
    db.commit()


@router.post("/{scenario_id}/restore", response_model=ScenarioOut)
def restore_scenario(scenario_id: str, principal: Principal = Depends(require_manage),
                     db: Session = Depends(get_db)) -> ScenarioOut:
    """Bring a deleted scenario back, exactly as it was."""
    scenario = svc.owned(db, principal.user, scenario_id, include_deleted=True)
    if scenario.deleted_at is None:
        raise HTTPException(status_code=422, detail="That scenario is not deleted")
    svc.restore(db, principal.user, scenario)
    db.commit()
    return _out(db, principal, scenario)


@router.delete("/{scenario_id}/purge", status_code=204)
def purge_scenario(scenario_id: str, principal: Principal = Depends(require_manage),
                   db: Session = Depends(get_db)) -> None:
    """Destroy a deleted scenario now instead of waiting out the window. This
    cannot be undone."""
    scenario = svc.owned(db, principal.user, scenario_id, include_deleted=True)
    svc.purge(db, principal.user, scenario)
    db.commit()


@router.get("/{scenario_id}/export")
def export_scenario(scenario_id: str, principal: Principal = Depends(require_read),
                    db: Session = Depends(get_db)) -> Response:
    """Download the whole track as JSON — accounts, holdings, ledger and rules.
    Statements are omitted; they re-render from the ledger after an import."""
    scenario = svc.owned(db, principal.user, scenario_id)
    payload = svc.export_scenario(db, principal.user, scenario)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in scenario.name).strip("-")
    filename = f"papertick-scenario-{safe or 'export'}-{utcnow().date().isoformat()}.json"
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
