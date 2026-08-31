"""Scenario tracks: independent copies of a user's accounts and history.

A scenario owns accounts; everything else (positions, lots, orders,
transactions, contributions, dividends, recurring rules, options) hangs off an
account, so scoping a request is one predicate on `Account.scenario_id`.
Statements are the exception — they aggregate across accounts, so they carry
the scenario id themselves.

Copying takes the *position*, not the past: balances and holdings come across,
priced at today's market so the new track starts flat, and the trade history,
dividends and auto-invest rules are deliberately left behind. A scenario is
"what happens from here", not a second copy of what already happened.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MAX_SCENARIOS_PER_USER,
    Account,
    CashFlowKind,
    Contribution,
    CostBasisOverride,
    Dividend,
    OptionPosition,
    OptionTransaction,
    Order,
    OrderSide,
    OrderSource,
    OrderStatus,
    OrderType,
    Position,
    QuantityType,
    RecurringRule,
    Scenario,
    Statement,
    TaxLot,
    Transaction,
    User,
    utcnow,
)
from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.scenarios")

ZERO = Decimal("0")
CENT = Decimal("0.01")
EXPORT_VERSION = 1

# child tables keyed by account_id, in delete-safe order
ACCOUNT_CHILDREN = (
    Transaction, TaxLot, Position, Dividend, Contribution,
    RecurringRule, CostBasisOverride, OptionTransaction, OptionPosition, Order,
)


def retention_days() -> int:
    from app.config import get_settings

    return get_settings().scenario_retention_days


def list_for(db: Session, user: User, include_deleted: bool = False) -> list[Scenario]:
    q = select(Scenario).where(Scenario.user_id == user.id)
    if not include_deleted:
        q = q.where(Scenario.deleted_at.is_(None))
    return list(db.execute(q.order_by(Scenario.sort_order, Scenario.created_at)).scalars())


def list_deleted(db: Session, user: User) -> list[Scenario]:
    return list(db.execute(
        select(Scenario)
        .where(Scenario.user_id == user.id, Scenario.deleted_at.isnot(None))
        .order_by(Scenario.deleted_at.desc())
    ).scalars())


def purges_at(scenario: Scenario) -> datetime | None:
    """When this deleted scenario stops being recoverable."""
    if scenario.deleted_at is None:
        return None
    return scenario.deleted_at + timedelta(days=retention_days())


def owned(db: Session, user: User, scenario_id: str,
          include_deleted: bool = False) -> Scenario:
    scenario = db.get(Scenario, scenario_id)
    if scenario is None or scenario.user_id != user.id:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if scenario.deleted_at is not None and not include_deleted:
        raise HTTPException(status_code=404, detail="Scenario has been deleted")
    return scenario


def frozen_accounts(db: Session):
    """Accounts inside deleted scenarios, as a subquery.

    A deleted scenario is frozen: its recurring buys, limit orders, settlement
    accrual and dividend reconciliation all stop, so nothing changes underneath
    a user who is deciding whether to restore it.
    """
    return (
        select(Account.id)
        .join(Scenario, Scenario.id == Account.scenario_id)
        .where(Scenario.deleted_at.isnot(None))
    )


def _unique_name(db: Session, user: User, wanted: str, exclude_id: str | None = None) -> str:
    """Names are unique per user; a collision gets a numeric suffix rather than
    an error, so importing the same file twice just works."""
    taken = {
        s.name for s in list_for(db, user, include_deleted=True) if s.id != exclude_id
    }
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted} ({n})" in taken:
        n += 1
    return f"{wanted} ({n})"


def create(db: Session, user: User, name: str, description: str | None = None,
           copy_from_id: str | None = None) -> Scenario:
    existing = list_for(db, user)
    if len(existing) >= MAX_SCENARIOS_PER_USER:
        raise HTTPException(
            status_code=422,
            detail=f"You already have the maximum of {MAX_SCENARIOS_PER_USER} scenarios.",
        )
    source = owned(db, user, copy_from_id) if copy_from_id else None
    scenario = Scenario(
        user_id=user.id,
        name=_unique_name(db, user, name.strip() or "Untitled scenario"),
        description=description,
        sort_order=max((s.sort_order for s in existing), default=-1) + 1,
        copied_from_id=source.id if source else None,
    )
    db.add(scenario)
    db.flush()
    if source is not None:
        _copy_positions(db, user, source, scenario)
    return scenario


def _price_of(ticker: str, fallback: Decimal) -> Decimal:
    try:
        return market_data.quote(ticker).price
    except MarketDataError:
        return fallback


def _copy_positions(db: Session, user: User, source: Scenario, target: Scenario) -> None:
    """Carry the balances and holdings across, and nothing else.

    Each copied account opens with a single contribution covering its cash plus
    the market value of its holdings, and one buy per holding dated today. That
    keeps the ledger self-consistent — the shares are explained by a trade, the
    trade is explained by a deposit — so performance measures the scenario from
    day one instead of inheriting gains that were earned elsewhere.
    """
    today = utcnow()
    as_of = today.date()
    stamp = datetime.combine(as_of, time(12, 0), tzinfo=timezone.utc)

    for account in db.execute(
        select(Account).where(Account.scenario_id == source.id)
        .order_by(Account.sort_order, Account.created_at)
    ).scalars():
        clone = Account(
            user_id=user.id,
            scenario_id=target.id,
            account_type=account.account_type,
            name=account.name,
            settlement_balance=Decimal(account.settlement_balance),
            settlement_accrued=ZERO,
            settlement_accrued_through=as_of,
            cost_basis_method=account.cost_basis_method,
            allow_external_funding=account.allow_external_funding,
            sort_order=account.sort_order,
        )
        db.add(clone)
        db.flush()

        positions = list(db.execute(
            select(Position).where(Position.account_id == account.id)
        ).scalars())
        holdings_value = ZERO
        priced: list[tuple[Position, Decimal, Decimal]] = []
        for pos in positions:
            shares = Decimal(pos.shares)
            if shares <= 0:
                continue
            price = _price_of(pos.ticker, Decimal(pos.average_cost))
            value = (shares * price).quantize(CENT)
            holdings_value += value
            priced.append((pos, shares, price))

        opening = Decimal(clone.settlement_balance) + holdings_value
        if opening > 0:
            db.add(Contribution(
                account_id=clone.id,
                tax_year=None,          # an opening balance is not a contribution year
                amount=opening,
                kind=CashFlowKind.ROLLOVER,
                memo=f"Opening balance copied from {source.name}",
                timestamp=stamp,
            ))
        # the cash was counted inside the opening deposit, so hold it all and
        # let each purchase draw it back down
        clone.settlement_balance = opening

        for pos, shares, price in priced:
            gross = (shares * price).quantize(CENT)
            order = Order(
                account_id=clone.id, ticker=pos.ticker, side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity_type=QuantityType.SHARES,
                quantity=shares, as_of=as_of, status=OrderStatus.FILLED,
                source=OrderSource.API, created_at=stamp,
            )
            db.add(order)
            db.flush()
            db.add(Transaction(
                order_id=order.id, account_id=clone.id, ticker=pos.ticker,
                side=OrderSide.BUY, executed_price=price, shares_filled=shares,
                gross_amount=gross, fees=ZERO, as_of=as_of, executed_at=stamp,
            ))
            db.add(TaxLot(account_id=clone.id, ticker=pos.ticker, shares_open=shares,
                          cost_per_share=price, acquired_on=as_of))
            db.add(Position(account_id=clone.id, ticker=pos.ticker, shares=shares,
                            average_cost=price))
            clone.settlement_balance = Decimal(clone.settlement_balance) - gross

    log.info("scenario %s copied holdings from %s", target.id, source.id)


def wipe(db: Session, scenario: Scenario) -> None:
    """Delete every row belonging to a scenario, leaving the scenario itself."""
    account_ids = [a.id for a in db.execute(
        select(Account).where(Account.scenario_id == scenario.id)
    ).scalars()]
    if account_ids:
        for model in ACCOUNT_CHILDREN:
            db.query(model).filter(model.account_id.in_(account_ids)).delete(
                synchronize_session=False)
        db.query(Account).filter(Account.id.in_(account_ids)).delete(
            synchronize_session=False)
    db.query(Statement).filter(Statement.scenario_id == scenario.id).delete(
        synchronize_session=False)
    db.flush()


def delete(db: Session, user: User, scenario: Scenario) -> None:
    """Soft delete: the data stays put for the retention window so the click is
    recoverable. `purge` is what actually destroys it."""
    if len(list_for(db, user)) <= 1:
        raise HTTPException(
            status_code=422,
            detail="This is your only scenario — create another before deleting it.",
        )
    scenario.deleted_at = utcnow()
    if user.default_scenario_id == scenario.id:
        remaining = [s for s in list_for(db, user) if s.id != scenario.id]
        user.default_scenario_id = remaining[0].id if remaining else None
    db.flush()


def restore(db: Session, user: User, scenario: Scenario) -> Scenario:
    """Bring a deleted scenario back, renaming it if the name was taken while
    it was away."""
    scenario.name = _unique_name(db, user, scenario.name, exclude_id=scenario.id)
    scenario.deleted_at = None
    db.flush()
    return scenario


def purge(db: Session, user: User, scenario: Scenario) -> None:
    """Destroy a deleted scenario and everything in it, now and for good."""
    if scenario.deleted_at is None:
        raise HTTPException(
            status_code=422,
            detail="Delete the scenario before purging it.",
        )
    wipe(db, scenario)
    if user.default_scenario_id == scenario.id:
        user.default_scenario_id = None
    db.delete(scenario)
    db.flush()
    if user.default_scenario_id is None:
        remaining = list_for(db, user)
        user.default_scenario_id = remaining[0].id if remaining else None


def purge_all(db: Session, user: User) -> int:
    deleted = list_deleted(db, user)
    for scenario in deleted:
        purge(db, user, scenario)
    return len(deleted)


def purge_expired(db: Session, now: datetime | None = None) -> int:
    """Beat task: wipe scenarios whose retention window has run out."""
    now = now or utcnow()
    cutoff = now - timedelta(days=retention_days())
    expired = db.execute(
        select(Scenario).where(
            Scenario.deleted_at.isnot(None), Scenario.deleted_at <= cutoff
        )
    ).scalars().all()
    for scenario in expired:
        user = db.get(User, scenario.user_id)
        wipe(db, scenario)
        if user is not None and user.default_scenario_id == scenario.id:
            user.default_scenario_id = None
        db.delete(scenario)
        log.info("purged expired scenario %s (%s)", scenario.id, scenario.name)
    if expired:
        db.flush()
    return len(expired)


# ------------------------------------------------------------------ transfer

def _plain(value):
    """JSON-safe scalar. Decimals become strings so cents survive the round
    trip, dates become ISO text, enums become their value."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        return None
    return str(value)


def _dump(row, skip: tuple[str, ...] = ()) -> dict:
    return {
        c.name: _plain(getattr(row, c.name))
        for c in row.__table__.columns
        if c.name not in skip
    }


def export_scenario(db: Session, user: User, scenario: Scenario) -> dict:
    """Everything needed to rebuild this track. Statements are left out on
    purpose: they are rendered from the ledger, so they come back on their own
    after an import rather than bloating the file with PDFs."""
    accounts = list(db.execute(
        select(Account).where(Account.scenario_id == scenario.id)
        .order_by(Account.sort_order, Account.created_at)
    ).scalars())
    ids = [a.id for a in accounts]

    def rows(model):
        if not ids:
            return []
        return list(db.execute(select(model).where(model.account_id.in_(ids))).scalars())

    return {
        "format": "papertick.scenario",
        "version": EXPORT_VERSION,
        "exported_at": utcnow().isoformat(),
        "scenario": {
            "name": scenario.name,
            "description": scenario.description,
        },
        "accounts": [_dump(a, skip=("user_id", "scenario_id")) for a in accounts],
        "orders": [_dump(o) for o in rows(Order)],
        "transactions": [_dump(t) for t in rows(Transaction)],
        "positions": [_dump(p) for p in rows(Position)],
        "tax_lots": [_dump(l) for l in rows(TaxLot)],
        "contributions": [_dump(c) for c in rows(Contribution)],
        "dividends": [_dump(d) for d in rows(Dividend)],
        "recurring_rules": [_dump(r) for r in rows(RecurringRule)],
        "cost_basis_overrides": [_dump(o) for o in rows(CostBasisOverride)],
        "option_positions": [_dump(p) for p in rows(OptionPosition)],
        "option_transactions": [_dump(t) for t in rows(OptionTransaction)],
    }


CHILD_TABLES = (
    ("orders", Order),
    ("transactions", Transaction),
    ("positions", Position),
    ("tax_lots", TaxLot),
    ("contributions", Contribution),
    ("dividends", Dividend),
    ("recurring_rules", RecurringRule),
    ("cost_basis_overrides", CostBasisOverride),
    ("option_positions", OptionPosition),
    ("option_transactions", OptionTransaction),
)


# every id column that points at another row inside the same export
CROSS_REFS = ("order_id", "recurring_rule_id", "exchange_from_order_id")


def _revive(column, value):
    """Turn an exported scalar back into what the column expects. JSON has no
    date, decimal or enum types, so each comes back as text and is rebuilt from
    the column definition rather than guessed at from the value."""
    if value is None:
        return None
    kind = column.type.__class__.__name__
    if kind == "Numeric" and isinstance(value, str):
        return Decimal(value)
    if kind == "DateTime" and isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if kind == "Date" and isinstance(value, str):
        return date.fromisoformat(value[:10])
    enum_class = getattr(column.type, "enum_class", None)
    if enum_class is not None and isinstance(value, str):
        return enum_class(value)
    return value


def _coerce(model, payload: dict) -> dict:
    """Keep the columns this build still has, revived to their column types."""
    columns = {c.name: c for c in model.__table__.columns}
    return {
        k: _revive(columns[k], v) for k, v in payload.items() if k in columns
    }


def import_scenario(db: Session, user: User, payload: dict,
                    target_id: str | None = None, name: str | None = None) -> Scenario:
    """Rebuild a scenario from an export. With `target_id` the existing
    scenario is emptied and replaced in place — that is the "restore over the
    top" path — otherwise a new one is created."""
    if not isinstance(payload, dict) or payload.get("format") != "papertick.scenario":
        raise HTTPException(status_code=422, detail="Not a PaperTick scenario export")
    version = payload.get("version")
    if version != EXPORT_VERSION:
        raise HTTPException(
            status_code=422,
            detail=f"Export version {version} is not supported by this build "
                   f"(expected {EXPORT_VERSION}).",
        )

    wanted = (name or (payload.get("scenario") or {}).get("name") or "Imported scenario").strip()
    if target_id:
        scenario = owned(db, user, target_id)
        wipe(db, scenario)
        if name:
            scenario.name = _unique_name(db, user, wanted, exclude_id=scenario.id)
    else:
        scenario = create(db, user, wanted,
                          (payload.get("scenario") or {}).get("description"))

    # New ids throughout: an export may be imported alongside the scenario it
    # came from, so primary keys cannot be reused. Rows go in first with their
    # original cross-references, then a second pass rewrites those through the
    # completed map — an order can name a recurring rule that is inserted after
    # it, and an exchange leg names another order in the same batch.
    id_map: dict[str, str] = {}
    inserted: list[tuple[object, dict]] = []

    for account in payload.get("accounts") or []:
        old_id = account.get("id")
        data = _coerce(Account, account)
        data.pop("id", None)
        row = Account(**data)
        row.user_id = user.id
        row.scenario_id = scenario.id
        db.add(row)
        db.flush()
        if old_id:
            id_map[old_id] = row.id

    for key, model in CHILD_TABLES:
        for payload_row in payload.get(key) or []:
            data = _coerce(model, payload_row)
            old_id = data.pop("id", None)
            mapped_account = id_map.get(data.get("account_id"))
            if mapped_account is None:
                continue               # points at an account the file omitted
            data["account_id"] = mapped_account
            row = model(**data)
            db.add(row)
            db.flush()
            if old_id:
                id_map[old_id] = row.id
            inserted.append((row, payload_row))

    for row, payload_row in inserted:
        for column in CROSS_REFS:
            original = payload_row.get(column)
            if original and hasattr(row, column):
                # unmapped means it referenced something outside the export;
                # dropping it is safer than pointing into another scenario
                setattr(row, column, id_map.get(original))

    db.flush()
    log.info("imported scenario %s (%s accounts) for %s",
             scenario.id, len(payload.get("accounts") or []), user.email)
    return scenario
