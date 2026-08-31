from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, owned_account, require_read, require_trade
from app.models import Account, Order, OrderSource, OrderStatus, Transaction
from app.rate_limit import rate_limiter
from app.schemas import (
    ExchangeIn,
    ExchangePreviewOut,
    ExchangeResultOut,
    OrderCreateIn,
    OrderOut,
    OrderResultOut,
    TransactionOut,
)
from app.services import trading

router = APIRouter(tags=["trading"])


@router.post("/orders", response_model=OrderResultOut, status_code=201,
             dependencies=[Depends(rate_limiter("orders", 60, 60))])
def place_order(data: OrderCreateIn, principal: Principal = Depends(require_trade),
                db: Session = Depends(get_db)) -> OrderResultOut:
    """Submit an order for immediate execution, a one-off future run
    (`scheduled_for`), or a backdated fill (`as_of`) — the two are mutually
    exclusive. A market order fills at the live quote immediately if the
    market is open, otherwise queues for the next NYSE open; a mutual fund
    order always waits for that day's closing NAV instead, regardless of
    market hours. A limit order rests as PENDING until the market crosses its
    price and is only checked during trading hours, while an `as_of` order
    fills right away at that date's historical, split-adjusted close and
    flows into today's balances, cost basis, and past statements."""
    account = owned_account(data.account_id, principal, db)
    source = OrderSource.API if principal.via_api_key else OrderSource.WEB
    order, txn = trading.place_order(db, account, data, source)
    return OrderResultOut(
        order=OrderOut.model_validate(order),
        transaction=TransactionOut.model_validate(txn) if txn else None,
        funding=getattr(order, "funding_note", None),
    )


@router.post("/orders/exchange/preview", response_model=ExchangePreviewOut)
def preview_exchange(data: ExchangeIn, principal: Principal = Depends(require_read),
                     db: Session = Depends(get_db)) -> ExchangePreviewOut:
    """Dry run of an exchange: what the sale realizes, lot by lot, and whether
    any of it is taxable. Writes nothing."""
    account = owned_account(data.account_id, principal, db)
    return trading.preview_exchange(db, account, data)


@router.post("/orders/exchange", response_model=ExchangeResultOut, status_code=201,
             dependencies=[Depends(rate_limiter("orders", 60, 60))])
def exchange(data: ExchangeIn, principal: Principal = Depends(require_trade),
             db: Session = Depends(get_db)) -> ExchangeResultOut:
    """Sell one holding and reinvest the proceeds in another, in one step."""
    account = owned_account(data.account_id, principal, db)
    source = OrderSource.API if principal.via_api_key else OrderSource.WEB
    sell, sell_txn, buy, buy_txn = trading.place_exchange(db, account, data, source)

    taxable = account.account_type.value == "TAXABLE"
    notes: list[str] = []
    if buy is None:
        notes.append(
            "The sale is queued; its proceeds buy "
            f"{data.to_ticker} automatically as soon as it fills."
        )
    elif buy.status.value == "REJECTED":
        notes.append(
            f"The sale filled but the {data.to_ticker} purchase did not: {buy.reject_reason} "
            "The proceeds are sitting in your settlement fund."
        )
    if sell_txn is not None and taxable:
        notes.append(
            "This sale is a taxable event — it appears on this year's tax report."
        )
    elif sell_txn is not None:
        notes.append("No tax impact: exchanges inside an IRA are not taxable events.")

    return ExchangeResultOut(
        sell=OrderResultOut(
            order=OrderOut.model_validate(sell),
            transaction=TransactionOut.model_validate(sell_txn) if sell_txn else None,
        ),
        buy=OrderResultOut(
            order=OrderOut.model_validate(buy),
            transaction=TransactionOut.model_validate(buy_txn) if buy_txn else None,
        ) if buy is not None else None,
        realized_gains=sell_txn.realized_gains if sell_txn and taxable else None,
        short_term_gains=sell_txn.realized_st if sell_txn and taxable else None,
        long_term_gains=sell_txn.realized_lt if sell_txn and taxable else None,
        taxable=taxable,
        notes=notes,
    )


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    account_id: str | None = None,
    status: OrderStatus | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
):
    """Orders across the caller's accounts in the current scenario, most
    recent first, optionally filtered to one account or one status."""
    q = (
        select(Order)
        .join(Account, Account.id == Order.account_id)
        .where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    if account_id:
        q = q.where(Order.account_id == account_id)
    if status:
        q = q.where(Order.status == status)
    return [OrderOut.model_validate(o) for o in db.execute(q).scalars()]


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: str, principal: Principal = Depends(require_read),
              db: Session = Depends(get_db)) -> OrderOut:
    """A single order by id, provided it belongs to an account the caller
    owns."""
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    owned_account(order.account_id, principal, db)
    return OrderOut.model_validate(order)


@router.delete("/orders/{order_id}", response_model=OrderOut)
def cancel_order(order_id: str, principal: Principal = Depends(require_trade),
                 db: Session = Depends(get_db)) -> OrderOut:
    """Cancel an order that has not filled yet. Only PENDING (resting limit)
    or SCHEDULED (queued for the next open, a NAV print, or a future time)
    orders can be cancelled — anything already filled or otherwise settled
    returns a conflict — and cancelling releases whatever cash or shares it
    had committed."""
    order = db.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    owned_account(order.account_id, principal, db)
    if order.status not in (OrderStatus.PENDING, OrderStatus.SCHEDULED):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a {order.status.value} order")
    order.status = OrderStatus.CANCELLED
    db.commit()
    return OrderOut.model_validate(order)


@router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(
    account_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    sort: Literal["executed", "effective"] = "executed",
    principal: Principal = Depends(require_read),
    db: Session = Depends(get_db),
):
    """`sort=executed` (default) is the audit view — when the fill actually ran.
    `sort=effective` orders by the date the trade is booked to (`as_of`), which
    is what an activity feed shows: a past-dated fill belongs at its own date,
    not at the top because it was entered today."""
    order_by = (
        (Transaction.as_of.desc(), Transaction.executed_at.desc())
        if sort == "effective"
        else (Transaction.executed_at.desc(),)
    )
    q = (
        select(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(Account.user_id == principal.user.id,
               Account.scenario_id == principal.scenario_id)
        .order_by(*order_by)
        .limit(limit)
    )
    if account_id:
        q = q.where(Transaction.account_id == account_id)
    return [TransactionOut.model_validate(t) for t in db.execute(q).scalars()]
