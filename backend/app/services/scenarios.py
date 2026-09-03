"""Scenario tracks: independent copies of a user's accounts and history.

A scenario owns accounts; everything else (positions, lots, orders,
transactions, contributions, dividends, recurring rules, options) hangs off an
account, so scoping a request is one predicate on `Account.scenario_id`.
Statements are the exception — they aggregate across accounts, so they carry
the scenario id themselves.

Copying comes in two shapes, because both are legitimate questions to ask of
a portfolio:

  position (default)
      Balances and holdings only, re-priced at today's market. Each copied
      account opens with one deposit and one buy per holding, so the new track
      starts flat and measures itself from day one. "What happens from here?"

  full
      An exact duplicate — every order, transaction, tax lot, dividend,
      contribution and auto-invest rule, with fresh ids. Returns and history
      carry over intact. "What if I had done something different?"

`full` is the same data an export/import round trip produces, minus the file:
statements are the only omission either way, because they re-render from the
ledger rather than being source data.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import security
from app.models import (
    MAX_SCENARIOS_PER_USER,
    Account,
    CashFlowKind,
    Contribution,
    Conversion,
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
    SplitApplication,
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
    SplitApplication,
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


FIRST_SCENARIO_NAME = "Scenario 1"


def ensure_default(db: Session, user: User) -> Scenario:
    """Give a user their first scenario, if they have none.

    An account is meaningless without one to hang off, so this runs the moment
    a user comes into existence rather than at the next backend start — the
    startup backfill only covers accounts that predate scenarios entirely.
    Idempotent: an existing scenario is adopted as the default instead.
    """
    existing = db.execute(
        select(Scenario)
        .where(Scenario.user_id == user.id, Scenario.deleted_at.is_(None))
        .order_by(Scenario.sort_order, Scenario.created_at)
    ).scalars().first()
    if existing is None:
        existing = Scenario(user_id=user.id, name=FIRST_SCENARIO_NAME, sort_order=0)
        db.add(existing)
        db.flush()
    if user.default_scenario_id is None:
        user.default_scenario_id = existing.id
    return existing


def create(db: Session, user: User, name: str, description: str | None = None,
           copy_from_id: str | None = None, copy_mode: str = "position") -> Scenario:
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
        if copy_mode == "full":
            _copy_full(db, user, source, scenario)
        else:
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

    That opening deposit is booked as OPENING_BALANCE, not as a rollover and
    not as a contribution. It consumes no annual IRA room (the money was
    already inside the wrapper), and it is not a reportable rollover — writing
    it as one added the entire copied account value to "rollovers received" on
    the tax summary.
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
                kind=CashFlowKind.OPENING_BALANCE,
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


def _copy_full(db: Session, user: User, source: Scenario, target: Scenario) -> None:
    """Duplicate the whole track, history included.

    Same row set and same id-remapping as an export/import round trip, done in
    process: rows are inserted carrying their original cross-references — which
    are still valid, since the source rows are right there in the same
    database — and a second pass rewrites them through the completed map. That
    ordering matters because an order can name a recurring rule that is
    inserted after it.
    """
    accounts = list(db.execute(
        select(Account).where(Account.scenario_id == source.id)
        .order_by(Account.sort_order, Account.created_at)
    ).scalars())
    account_ids = [a.id for a in accounts]
    id_map: dict[str, str] = {}

    for account in accounts:
        data = {
            c.name: getattr(account, c.name)
            for c in Account.__table__.columns
            if c.name not in ("id", "user_id", "scenario_id")
        }
        clone = Account(**data, user_id=user.id, scenario_id=target.id)
        db.add(clone)
        db.flush()
        id_map[account.id] = clone.id

    inserted: list[tuple[object, object]] = []
    if account_ids:
        for _key, model in CHILD_TABLES:
            rows = db.execute(
                select(model).where(model.account_id.in_(account_ids))
            ).scalars().all()
            for row in rows:
                data = {
                    c.name: getattr(row, c.name)
                    for c in model.__table__.columns if c.name != "id"
                }
                data["account_id"] = id_map[row.account_id]
                clone = model(**data)
                db.add(clone)
                db.flush()
                id_map[row.id] = clone.id
                inserted.append((clone, row))

    # two-account rows go in once every account they join has been mapped
    if account_ids:
        for _key, model, cols in ACCOUNT_PAIR_TABLES:
            for row in db.execute(select(model).where(or_(*[
                getattr(model, c).in_(account_ids) for c in cols
            ]))).scalars().all():
                data = {c.name: getattr(row, c.name)
                        for c in model.__table__.columns if c.name != "id"}
                if any(data.get(c) not in id_map for c in cols):
                    continue           # joins an account outside this scenario
                for c in cols:
                    data[c] = id_map[data[c]]
                db.add(model(**data))

    for clone, original in inserted:
        for column in CROSS_REFS:
            value = getattr(original, column, None)
            if value:
                setattr(clone, column, id_map.get(value))

    target.allow_backdated = source.allow_backdated
    db.flush()
    log.info("scenario %s fully copied from %s (%d accounts, %d rows)",
             target.id, source.id, len(accounts), len(inserted))


def wipe(db: Session, scenario: Scenario) -> None:
    """Delete every row belonging to a scenario, leaving the scenario itself."""
    account_ids = [a.id for a in db.execute(
        select(Account).where(Account.scenario_id == scenario.id)
    ).scalars()]
    if account_ids:
        for _key, model, cols in ACCOUNT_PAIR_TABLES:
            db.query(model).filter(or_(*[
                getattr(model, c).in_(account_ids) for c in cols
            ])).delete(synchronize_session=False)
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

    body = {
        "format": "papertick.scenario",
        "version": EXPORT_VERSION,
        "exported_at": utcnow().isoformat(),
        "scenario": {
            "name": scenario.name,
            "description": scenario.description,
            "allow_backdated": scenario.allow_backdated,
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
        # carried so a copy does not re-apply splits its lots already reflect
        "split_applications": [_dump(a) for a in rows(SplitApplication)],
        "option_positions": [_dump(p) for p in rows(OptionPosition)],
        "option_transactions": [_dump(t) for t in rows(OptionTransaction)],
        # carried so a restored Roth still knows which of its money was
        # converted, when, and how much of it was already taxed
        "conversions": [
            _dump(c) for c in (db.execute(select(Conversion).where(or_(
                Conversion.from_account_id.in_(ids),
                Conversion.to_account_id.in_(ids),
            ))).scalars() if ids else [])
        ],
    }
    # Detached: the signature covers the body but is not part of it, so
    # verification re-signs exactly what was signed.
    return {**body, "signature": security.sign_export(body)}


#: Tables keyed by *two* account columns instead of one. A conversion joins the
#: account money left with the account it arrived in, so neither column alone
#: identifies it and the generic `account_id` machinery cannot carry it. Left
#: out of an export, a restored scenario would forget its conversion history —
#: and Roth ordering would then treat already-taxed money as earnings.
ACCOUNT_PAIR_TABLES = (
    ("conversions", Conversion, ("from_account_id", "to_account_id")),
)

CHILD_TABLES = (
    ("orders", Order),
    ("transactions", Transaction),
    ("positions", Position),
    ("tax_lots", TaxLot),
    ("contributions", Contribution),
    ("dividends", Dividend),
    ("recurring_rules", RecurringRule),
    ("cost_basis_overrides", CostBasisOverride),
    ("split_applications", SplitApplication),
    ("option_positions", OptionPosition),
    ("option_transactions", OptionTransaction),
)


# every id column that points at another row inside the same export
CROSS_REFS = ("order_id", "recurring_rule_id", "exchange_from_order_id")


# An import is signed (see `security.sign_export`), so a payload that reaches
# the row loop was produced by this deployment. These bounds are the second
# layer: they turn a corrupt or stale-but-signed file into a clean 422 instead
# of a 500, a poisoned column, or an unbounded allocation.
MAX_ROWS_PER_TABLE = 25_000
MAX_ACCOUNTS = 100
MAX_MAGNITUDE = Decimal("1e12")
MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2200, 1, 1)


def _bad(field: str, why: str):
    return HTTPException(status_code=422, detail=f"Invalid export: {field} {why}")


def _revive(column, value):
    """Turn an exported scalar back into what the column expects, rejecting
    anything the column cannot hold. JSON has no date, decimal or enum types,
    so each comes back as text and is rebuilt from the column definition rather
    than guessed at from the value."""
    if value is None:
        return None
    kind = column.type.__class__.__name__
    name = column.name

    if kind == "Numeric":
        if isinstance(value, float):
            raise _bad(name, "must be a decimal string, not a float")
        try:
            d = Decimal(value) if isinstance(value, (str, int)) else None
        except (InvalidOperation, ValueError):
            raise _bad(name, "is not a valid decimal")
        if d is None:
            raise _bad(name, "is not a valid decimal")
        # NaN and Infinity are accepted by Decimal() and by PostgreSQL NUMERIC.
        # Stored, they poison every later balance sum and turn the account list
        # into a permanent 500.
        if not d.is_finite():
            raise _bad(name, "must be a finite number")
        if abs(d) > MAX_MAGNITUDE:
            raise _bad(name, f"exceeds the maximum magnitude of {MAX_MAGNITUDE:g}")
        return d

    if kind == "DateTime":
        if not isinstance(value, str):
            raise _bad(name, "must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise _bad(name, "is not a valid timestamp")
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        if not (MIN_DATE <= parsed.date() <= MAX_DATE):
            raise _bad(name, "is outside the supported date range")
        return parsed

    if kind == "Date":
        if not isinstance(value, str):
            raise _bad(name, "must be an ISO-8601 date")
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            raise _bad(name, "is not a valid date")
        if not (MIN_DATE <= parsed <= MAX_DATE):
            raise _bad(name, "is outside the supported date range")
        return parsed

    enum_class = getattr(column.type, "enum_class", None)
    if enum_class is not None:
        if not isinstance(value, str):
            raise _bad(name, "must be a string")
        try:
            return enum_class(value)
        except ValueError:
            raise _bad(name, f"is not one of {[e.value for e in enum_class]}")

    if kind in ("String", "Text"):
        if not isinstance(value, str):
            raise _bad(name, "must be a string")
        limit = getattr(column.type, "length", None)
        if limit is not None and len(value) > limit:
            raise _bad(name, f"is longer than {limit} characters")
        if "\x00" in value:
            raise _bad(name, "contains a NUL byte")
        return value

    if kind == "Integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _bad(name, "must be an integer")
        if not (-2**31 < value < 2**31):
            raise _bad(name, "is out of range")
        return value

    if kind == "Boolean":
        if not isinstance(value, bool):
            raise _bad(name, "must be true or false")
        return value

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

    # An import writes straight into the ledger: balances, contributions,
    # realized gains, tax lots. Only a file this deployment produced may do
    # that, so the signature is checked before a single row is read.
    body = {k: v for k, v in payload.items() if k != "signature"}
    if not security.verify_export(body, payload.get("signature")):
        raise HTTPException(
            status_code=422,
            detail=(
                "This file is not a valid PaperTick export: its signature is "
                "missing or does not match. Import only files produced by this "
                "deployment's scenario export, unmodified."
            ),
        )

    if len(payload.get("accounts") or []) > MAX_ACCOUNTS:
        raise HTTPException(status_code=422,
                            detail=f"Export holds more than {MAX_ACCOUNTS} accounts")
    for key, _model in CHILD_TABLES + tuple((k, m) for k, m, _ in ACCOUNT_PAIR_TABLES):
        if len(payload.get(key) or []) > MAX_ROWS_PER_TABLE:
            raise HTTPException(
                status_code=422,
                detail=f"Export holds more than {MAX_ROWS_PER_TABLE:,} {key} rows",
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
    # absent in files written before this was a per-scenario setting
    scenario.allow_backdated = bool((payload.get("scenario") or {}).get("allow_backdated"))

    # New ids throughout: an export may be imported alongside the scenario it
    # came from, so primary keys cannot be reused. Rows go in first with their
    # original cross-references, then a second pass rewrites those through the
    # completed map — an order can name a recurring rule that is inserted after
    # it, and an exchange leg names another order in the same batch.
    id_map: dict[str, str] = {}
    inserted: list[tuple[object, dict]] = []

    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            raise HTTPException(status_code=422, detail="Invalid export: account row is not an object")
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
            if not isinstance(payload_row, dict):
                raise HTTPException(status_code=422,
                                    detail=f"Invalid export: {key} row is not an object")
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

    for key, model, cols in ACCOUNT_PAIR_TABLES:
        for payload_row in payload.get(key) or []:
            if not isinstance(payload_row, dict):
                raise HTTPException(status_code=422,
                                    detail=f"Invalid export: {key} row is not an object")
            data = _coerce(model, payload_row)
            data.pop("id", None)
            mapped = {c: id_map.get(data.get(c)) for c in cols}
            if any(v is None for v in mapped.values()):
                continue               # joins an account the file omitted
            data.update(mapped)
            db.add(model(**data))

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
