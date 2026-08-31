from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, owned_account, require_manage, require_read, require_trade
from app.models import (
    Account,
    AccountType,
    Asset,
    AssetClass,
    CashFlowKind,
    Contribution,
    CostBasisMethod,
    CostBasisOverride,
)
from app.schemas import (
    AccountCreateIn,
    AccountOrderIn,
    AccountOut,
    AccountSettingsIn,
    CashFlowResultOut,
    ContributionOut,
    CostBasisConfigOut,
    CostBasisOverrideOut,
    CostBasisUpdateIn,
    DepositIn,
    IrsStatusOut,
    WithdrawIn,
)
from app.services import irs

router = APIRouter(tags=["accounts"])

ACCOUNT_TYPE_LABEL = {
    AccountType.TAXABLE: "Taxable Brokerage",
    AccountType.ROTH_IRA: "Roth IRA",
    AccountType.TRADITIONAL_IRA: "Traditional IRA",
    AccountType.ROLLOVER_IRA: "Rollover IRA",
}


from app.services.trading import account_out as _account_out


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(principal: Principal = Depends(require_read), db: Session = Depends(get_db)):
    """Lists every account in the caller's active scenario, in the user's saved
    display order. Each entry is enriched with live buying power and
    settlement-fund detail, not just the raw balance."""
    rows = db.execute(
        select(Account).where(Account.user_id == principal.user.id,
                              Account.scenario_id == principal.scenario_id)
        .order_by(Account.sort_order, Account.created_at)
    ).scalars().all()
    return [_account_out(db, a) for a in rows]


@router.post("/accounts", response_model=AccountOut, status_code=201)
def create_account(data: AccountCreateIn, principal: Principal = Depends(require_manage),
                   db: Session = Depends(get_db)) -> AccountOut:
    """Opens a new account bucket of the given type. Only one account per type
    is allowed per user per scenario — TAXABLE, ROTH_IRA, TRADITIONAL_IRA and
    ROLLOVER_IRA each get at most one, since the IRS treats each type as a
    single contribution/withdrawal regime; a duplicate returns 409."""
    existing = db.execute(
        select(Account).where(Account.user_id == principal.user.id,
                              Account.scenario_id == principal.scenario_id)
    ).scalars().all()
    # One bucket per account type: the IRS treats each type as a single
    # contribution/withdrawal regime, and duplicates only split the same rules
    # across rows that then have to be reconciled.
    if any(a.account_type == data.account_type for a in existing):
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have a {ACCOUNT_TYPE_LABEL[data.account_type]} account. "
                "One account per type — rename the existing one instead."
            ),
        )
    account = Account(
        user_id=principal.user.id,
        scenario_id=principal.scenario_id,
        account_type=data.account_type,
        name=data.name,
        sort_order=max((a.sort_order or 0) for a in existing) + 1 if existing else 0,
    )
    db.add(account)
    db.commit()
    return _account_out(db, account)


@router.put("/accounts/order", response_model=list[AccountOut])
def reorder_accounts(data: AccountOrderIn, principal: Principal = Depends(require_manage),
                     db: Session = Depends(get_db)):
    """Persist the user's preferred display order (drag and drop)."""
    owned = {
        a.id: a for a in db.execute(
            select(Account).where(Account.user_id == principal.user.id,
                                  Account.scenario_id == principal.scenario_id)
        ).scalars()
    }
    unknown = [i for i in data.account_ids if i not in owned]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Unknown account {unknown[0]}")
    if len(set(data.account_ids)) != len(data.account_ids):
        raise HTTPException(status_code=422, detail="Duplicate account in the ordering")
    for position, account_id in enumerate(data.account_ids):
        owned[account_id].sort_order = position
    # anything the client left out keeps a stable position after the listed ones
    for offset, account in enumerate(
        sorted((a for i, a in owned.items() if i not in set(data.account_ids)),
               key=lambda a: (a.sort_order, a.created_at))
    ):
        account.sort_order = len(data.account_ids) + offset
    db.commit()
    return list_accounts(principal, db)


@router.get("/accounts/{account_id}", response_model=AccountOut)
def get_account(account_id: str, principal: Principal = Depends(require_read),
                db: Session = Depends(get_db)) -> AccountOut:
    """Returns one account, enriched with live buying power and settlement-fund
    detail."""
    return _account_out(db, owned_account(account_id, principal, db))


@router.patch("/accounts/{account_id}", response_model=AccountOut)
def update_account(account_id: str, data: AccountSettingsIn,
                   principal: Principal = Depends(require_manage),
                   db: Session = Depends(get_db)) -> AccountOut:
    """Updates the account's display name and/or whether external bank funding
    is allowed. Only the fields provided are changed; omitted fields are left
    as-is."""
    account = owned_account(account_id, principal, db)
    if data.name is not None:
        account.name = data.name
    if data.allow_external_funding is not None:
        account.allow_external_funding = data.allow_external_funding
    db.commit()
    return _account_out(db, account)


@router.post("/accounts/{account_id}/deposit", response_model=CashFlowResultOut)
def deposit(account_id: str, data: DepositIn, principal: Principal = Depends(require_trade),
            db: Session = Depends(get_db)) -> CashFlowResultOut:
    """Adds cash to the account's settlement fund and logs a Contribution row.
    For Roth/Traditional IRAs, the amount is checked against the shared annual
    IRS contribution limit — blocked with a 422 if it would be exceeded, or
    flagged with a warning once less than 10% of that year's limit remains —
    and defaults to the prior tax year while it's still open (before Tax Day)
    unless `tax_year` is given. TAXABLE deposits and ROLLOVER-kind deposits
    skip limit checks entirely; a regular contribution into a Rollover IRA is
    rejected outright."""
    account = owned_account(account_id, principal, db)
    kind = CashFlowKind(data.kind)
    tax_year, warnings, irs_after = irs.validate_deposit(
        db, principal.user, account, data.amount, data.tax_year, kind
    )
    locked = db.execute(
        select(Account).where(Account.id == account.id).with_for_update()
    ).scalar_one()
    locked.settlement_balance = Decimal(locked.settlement_balance) + data.amount
    contribution = Contribution(
        account_id=locked.id, tax_year=tax_year, amount=data.amount, kind=kind
    )
    db.add(contribution)
    db.commit()
    return CashFlowResultOut(
        account=_account_out(db, locked),
        contribution=ContributionOut.model_validate(contribution),
        warnings=warnings,
        irs=irs_after,
    )


@router.post("/accounts/{account_id}/withdraw", response_model=CashFlowResultOut)
def withdraw(account_id: str, data: WithdrawIn, principal: Principal = Depends(require_trade),
             db: Session = Depends(get_db)) -> CashFlowResultOut:
    """Removes cash from the account's settlement fund. The amount available to
    withdraw excludes cash committed to open buy orders or held as short-put
    collateral, even though the full balance is what actually gets debited;
    requests over that available amount are blocked with a 422. Withdrawing
    from an IRA before age 59½ (per the user's date of birth) adds a warning
    about early-withdrawal taxes and penalty, but is not blocked."""
    from app.services.trading import buying_power

    account = owned_account(account_id, principal, db)
    locked = db.execute(
        select(Account).where(Account.id == account.id).with_for_update()
    ).scalar_one()
    balance = Decimal(locked.settlement_balance)
    # cash backing open orders or short-put collateral is not withdrawable
    withdrawable = buying_power(db, locked.id)
    if data.amount > withdrawable:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Insufficient settlement fund balance: requested ${data.amount}, "
                f"available ${withdrawable} (money committed to open orders or held as"
                " short-put collateral cannot be withdrawn; everything else in the"
                " settlement fund is available immediately)"
            ),
        )
    warnings: list[str] = []
    if locked.account_type != AccountType.TAXABLE:
        age = (date.today() - principal.user.date_of_birth).days / 365.25
        if age < 59.5:
            warnings.append(
                "Early IRA withdrawal before age 59½ — in a real account this could "
                "trigger taxes and a 10% penalty."
            )
    locked.settlement_balance = balance - data.amount  # full balance, not the withdrawable subset
    contribution = Contribution(
        account_id=locked.id, tax_year=None, amount=-data.amount, kind=CashFlowKind.WITHDRAWAL
    )
    db.add(contribution)
    db.commit()
    return CashFlowResultOut(
        account=_account_out(db, locked),
        contribution=ContributionOut.model_validate(contribution),
        warnings=warnings,
        irs=None,
    )


@router.get("/accounts/{account_id}/contributions", response_model=list[ContributionOut])
def list_contributions(account_id: str, principal: Principal = Depends(require_read),
                       db: Session = Depends(get_db)):
    """Returns this account's most recent cash-flow history — contributions,
    withdrawals and rollovers — newest first, capped at 500 rows."""
    account = owned_account(account_id, principal, db)
    rows = db.execute(
        select(Contribution)
        .where(Contribution.account_id == account.id)
        .order_by(Contribution.timestamp.desc())
        .limit(500)
    ).scalars().all()
    return [ContributionOut.model_validate(c) for c in rows]


# ------------------------------------------------------------------ cost basis

def _average_locked(db: Session, account_id: str, ticker: str) -> bool:
    """True once any sale of this fund has executed under average cost — the
    averaged basis of those shares is then permanent (IRS §1.1012-1(e))."""
    from app.models import Order, OrderSide, OrderStatus

    hit = db.execute(
        select(Order.id).where(
            Order.account_id == account_id,
            Order.ticker == ticker,
            Order.side == OrderSide.SELL,
            Order.status == OrderStatus.FILLED,
            Order.cost_basis_method == CostBasisMethod.AVERAGE,
        ).limit(1)
    ).first()
    return hit is not None


CB_NOTES = [
    "Cost-basis elections apply to taxable brokerage accounts only.",
    "FIFO, HIFO, MinTax, LIFO and SpecID can be changed at any time; changes apply to future sales.",
    "Average cost (mutual funds only) is revocable until the first sale under it; after that "
    "sale the averaged basis of existing shares is permanent — only shares purchased after "
    "switching methods get their actual cost (IRS §1.1012-1(e)).",
]


@router.get("/accounts/{account_id}/cost-basis", response_model=CostBasisConfigOut)
def get_cost_basis(account_id: str, principal: Principal = Depends(require_read),
                   db: Session = Depends(get_db)) -> CostBasisConfigOut:
    """Returns the account's default cost-basis method and any per-ticker
    overrides. Each mutual-fund override also reports whether its average-cost
    election is already locked in by a prior sale under that method, since a
    locked election can no longer be changed for the existing shares (IRS
    §1.1012-1(e))."""
    account = owned_account(account_id, principal, db)
    overrides = db.execute(
        select(CostBasisOverride).where(CostBasisOverride.account_id == account.id)
        .order_by(CostBasisOverride.ticker)
    ).scalars().all()
    return CostBasisConfigOut(
        account_id=account.id,
        default_method=account.cost_basis_method or CostBasisMethod.FIFO,
        overrides=[
            CostBasisOverrideOut(
                ticker=o.ticker,
                method=o.method,
                average_locked=(
                    o.method == CostBasisMethod.AVERAGE
                    and _average_locked(db, account.id, o.ticker)
                ),
            )
            for o in overrides
        ],
        notes=CB_NOTES,
    )


def _require_taxable(account) -> None:
    if account.account_type != AccountType.TAXABLE:
        raise HTTPException(
            status_code=422,
            detail="Cost-basis elections apply only to taxable brokerage accounts — "
                   "IRA sales have no capital-gains treatment and always use FIFO",
        )


@router.put("/accounts/{account_id}/cost-basis", response_model=CostBasisConfigOut)
def set_cost_basis(account_id: str, data: CostBasisUpdateIn,
                   principal: Principal = Depends(require_manage),
                   db: Session = Depends(get_db)) -> CostBasisConfigOut:
    """Sets the account's default cost-basis method, or a per-ticker override
    when `ticker` is given. Only taxable brokerage accounts support elections
    (IRAs always use FIFO); average cost can only be elected per mutual fund,
    never as the account-wide default. Changes apply to future sales only —
    once the first sale executes under an average-cost election, the averaged
    basis of the existing shares becomes permanent."""
    account = owned_account(account_id, principal, db)
    _require_taxable(account)
    if data.ticker is None:
        if data.method == CostBasisMethod.AVERAGE:
            raise HTTPException(
                status_code=422,
                detail="Average cost can only be elected per mutual fund, not account-wide (IRS rule)",
            )
        account.cost_basis_method = data.method
    else:
        ticker = data.ticker.upper()
        asset = db.get(Asset, ticker)
        if asset is None:
            raise HTTPException(status_code=422, detail=f"Unknown ticker {ticker!r}")
        if data.method == CostBasisMethod.AVERAGE and asset.asset_class != AssetClass.MUTUAL_FUND:
            raise HTTPException(
                status_code=422,
                detail="Average cost is only permitted for mutual funds (IRS rule)",
            )
        override = db.execute(
            select(CostBasisOverride).where(
                CostBasisOverride.account_id == account.id, CostBasisOverride.ticker == ticker
            )
        ).scalar_one_or_none()
        if override is None:
            db.add(CostBasisOverride(account_id=account.id, ticker=ticker, method=data.method))
        else:
            override.method = data.method
    db.commit()
    return get_cost_basis(account_id, principal, db)


@router.delete("/accounts/{account_id}/cost-basis/{ticker}", response_model=CostBasisConfigOut)
def clear_cost_basis_override(account_id: str, ticker: str,
                              principal: Principal = Depends(require_manage),
                              db: Session = Depends(get_db)) -> CostBasisConfigOut:
    """Removes a per-ticker cost-basis override, reverting that fund to the
    account's default method. Only taxable brokerage accounts support
    elections; this is a no-op if no override exists for the ticker."""
    account = owned_account(account_id, principal, db)
    _require_taxable(account)
    db.query(CostBasisOverride).filter(
        CostBasisOverride.account_id == account.id,
        CostBasisOverride.ticker == ticker.upper(),
    ).delete()
    db.commit()
    return get_cost_basis(account_id, principal, db)


# ------------------------------------------------------------------ IRS

@router.get("/irs/status", response_model=IrsStatusOut, tags=["irs"])
def irs_limit_status(
    tax_year: int | None = Query(default=None, ge=2000, le=2100),
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
) -> IrsStatusOut:
    """Returns the user's shared IRA contribution limit, amount contributed and
    remaining room for one tax year (defaults to the current year), scoped to
    the active scenario. This is the aggregate across all of the user's Roth
    and Traditional IRAs, not any single account — a Rollover IRA does not
    participate in this limit."""
    year = tax_year if tax_year is not None else date.today().year
    return irs.irs_status(db, principal.user, year, principal.scenario_id)


@router.get("/irs/allowed-years", tags=["irs"])
def irs_allowed_years(principal: Principal = Depends(require_read),
                      db: Session = Depends(get_db)) -> dict:
    """Buckets a contribution may be designated to right now. `default_tax_year`
    is the prior year while it is open with room left — that room lapses at Tax
    Day, so it is the one to spend first."""
    today = date.today()
    buckets = irs.open_tax_years(db, principal.user, today, principal.scenario_id)
    return {
        "allowed_tax_years": irs.allowed_tax_years(db, today),
        "default_tax_year": irs.default_tax_year(db, principal.user, today,
                                                 principal.scenario_id),
        "buckets": [
            {
                "tax_year": year,
                "remaining": str(remaining),
                "designation_deadline": deadline.isoformat(),
                "is_prior_year": year < today.year,
            }
            for year, remaining, deadline in buckets
        ],
    }
