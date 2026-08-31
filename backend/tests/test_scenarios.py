"""Scenario tracks: isolation, copy semantics, and the export/import round trip."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    Order,
    OrderSource,
    Position,
    Scenario,
    TaxLot,
    Transaction,
    User,
)
from app.schemas import OrderCreateIn
from app.services import metrics, scenarios, trading


def _buy(db, account, dollars="5000", days_ago=None):
    return trading.place_order(
        db, account,
        OrderCreateIn(account_id=account.id, ticker="VOO", side="BUY",
                      quantity_type="DOLLARS", quantity=Decimal(dollars),
                      as_of=(date.today() - timedelta(days=days_ago)) if days_ago else None),
        OrderSource.API,
    )


# ---------------------------------------------------------------- isolation

def test_scenarios_are_isolated(db, user, taxable, scenario):
    _buy(db, taxable)
    db.commit()

    other = scenarios.create(db, user, "What if")
    db.commit()
    fresh = Account(user_id=user.id, scenario_id=other.id, account_type=AccountType.TAXABLE,
                    name="Brokerage", settlement_balance=Decimal("2500"))
    db.add(fresh)
    db.commit()

    # each track sees only its own accounts and holdings
    base = metrics.summary(db, user, None, scenario.id)
    alt = metrics.summary(db, user, None, other.id)
    assert [a.id for a in base.accounts] == [taxable.id]
    assert [a.id for a in alt.accounts] == [fresh.id]
    assert base.invested_value > 0 and alt.invested_value == 0
    assert alt.cash == Decimal("2500.00")
    # and an unscoped call still sees everything, which is what workers need
    assert len(metrics.summary(db, user, None, None).accounts) == 2


def test_ira_limit_is_tracked_per_scenario(db, user, roth, limits, scenario):
    from app.services import irs

    year = date.today().year
    db.add(Contribution(account_id=roth.id, tax_year=year, amount=Decimal("4000"),
                        kind=CashFlowKind.CONTRIBUTION))
    db.commit()

    other = scenarios.create(db, user, "Aggressive")
    db.commit()
    alt_roth = Account(user_id=user.id, scenario_id=other.id,
                       account_type=AccountType.ROTH_IRA, name="Roth",
                       settlement_balance=Decimal("0"))
    db.add(alt_roth)
    db.commit()

    assert irs.contributed_for_year(db, user, year, scenario.id) == Decimal("4000")
    # the what-if starts with its full room, not the real track's leftovers
    assert irs.contributed_for_year(db, user, year, other.id) == Decimal("0")
    assert irs.contribution_statuses(db, alt_roth)[0].remaining == \
        irs.contribution_statuses(db, alt_roth)[0].limit


def test_account_from_another_scenario_is_not_found(db, user, taxable, scenario):
    from app.deps import Principal, owned_account

    other = scenarios.create(db, user, "Other")
    db.commit()
    principal = Principal(user=user, scopes={"read"}, via_api_key=False, scenario=other)
    with pytest.raises(HTTPException) as exc:
        owned_account(taxable.id, principal, db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- copying

def test_copy_brings_balances_and_holdings_but_no_history(db, user, taxable, scenario):
    _buy(db, taxable, "5000", days_ago=400)
    db.commit()
    source_shares = db.query(Position).filter_by(account_id=taxable.id).one().shares

    copy = scenarios.create(db, user, "From here", copy_from_id=scenario.id)
    db.commit()

    accounts = db.query(Account).filter_by(scenario_id=copy.id).all()
    assert len(accounts) == 1
    clone = accounts[0]
    assert clone.name == taxable.name and clone.account_type == taxable.account_type

    # the holding came across, share for share
    pos = db.query(Position).filter_by(account_id=clone.id).one()
    assert Decimal(pos.shares) == Decimal(source_shares)

    # …priced at today, so the scenario starts flat rather than inheriting gains
    lot = db.query(TaxLot).filter_by(account_id=clone.id).one()
    assert lot.acquired_on == date.today()
    view = metrics.positions_view(db, user, None, copy.id)[0]
    assert abs(view.unrealized_gains) < Decimal("0.02")

    # the ledger explains the shares: one opening deposit, one opening buy
    assert db.query(Contribution).filter_by(account_id=clone.id).count() == 1
    assert db.query(Transaction).filter_by(account_id=clone.id).count() == 1
    # and nothing from the source's past came with it
    assert db.query(Order).filter_by(account_id=clone.id).count() == 1

    # total value matches the source it was taken from
    assert abs(metrics.summary(db, user, None, copy.id).total_value
               - metrics.summary(db, user, None, scenario.id).total_value) < Decimal("0.02")


def test_copy_drops_recurring_rules(db, user, taxable, scenario):
    from app.models import Cadence, RecurringRule, utcnow

    db.add(RecurringRule(account_id=taxable.id, ticker="VOO", amount=Decimal("100"),
                         cadence=Cadence.WEEKLY, day_of_week=0, next_run_at=utcnow()))
    db.commit()
    copy = scenarios.create(db, user, "No autopilot", copy_from_id=scenario.id)
    db.commit()

    ids = [a.id for a in db.query(Account).filter_by(scenario_id=copy.id).all()]
    assert db.query(RecurringRule).filter(RecurringRule.account_id.in_(ids)).count() == 0


# ---------------------------------------------------------------- transfer

def test_export_import_round_trip(db, user, taxable, scenario):
    _buy(db, taxable, "5000", days_ago=400)
    db.commit()
    before = metrics.summary(db, user, None, scenario.id)

    payload = scenarios.export_scenario(db, user, scenario)
    assert payload["format"] == "papertick.scenario"
    assert payload["accounts"] and payload["transactions"]

    restored = scenarios.import_scenario(db, user, payload, name="Restored")
    db.commit()

    after = metrics.summary(db, user, None, restored.id)
    assert after.total_value == before.total_value
    assert after.cost_basis == before.cost_basis
    assert len(after.accounts) == len(before.accounts)
    # rows were re-issued rather than reused
    new_ids = {a.id for a in after.accounts}
    assert not (new_ids & {a.id for a in before.accounts})
    # the transaction still points at its own order, inside the new scenario
    txn = db.query(Transaction).filter_by(account_id=list(new_ids)[0]).first()
    assert db.get(Order, txn.order_id).account_id == txn.account_id


def test_import_over_an_existing_scenario_replaces_it(db, user, taxable, scenario):
    _buy(db, taxable, "5000")
    db.commit()
    payload = scenarios.export_scenario(db, user, scenario)

    target = scenarios.create(db, user, "Scratch")
    db.commit()
    db.add(Account(user_id=user.id, scenario_id=target.id, account_type=AccountType.ROTH_IRA,
                   name="Doomed", settlement_balance=Decimal("999")))
    db.commit()

    scenarios.import_scenario(db, user, payload, target_id=target.id)
    db.commit()

    names = [a.name for a in db.query(Account).filter_by(scenario_id=target.id).all()]
    assert "Doomed" not in names and names == [taxable.name]


def test_import_rejects_a_foreign_file(db, user, scenario):
    with pytest.raises(HTTPException) as exc:
        scenarios.import_scenario(db, user, {"format": "something-else"})
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        scenarios.import_scenario(db, user, {"format": "papertick.scenario", "version": 99})
    assert exc.value.status_code == 422


# ---------------------------------------------------------------- lifecycle

def test_names_are_unique_and_the_last_scenario_cannot_be_deleted(db, user, scenario):
    first = scenarios.create(db, user, "Plan")
    second = scenarios.create(db, user, "Plan")
    db.commit()
    assert first.name == "Plan" and second.name == "Plan (2)"

    scenarios.delete(db, user, first)
    scenarios.delete(db, user, second)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        scenarios.delete(db, user, scenario)
    assert exc.value.status_code == 422


def test_scenario_cap_is_enforced(db, user, scenario, monkeypatch):
    monkeypatch.setattr(scenarios, "MAX_SCENARIOS_PER_USER", 3)
    scenarios.create(db, user, "Two")
    scenarios.create(db, user, "Three")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        scenarios.create(db, user, "Four")
    assert exc.value.status_code == 422
    assert "maximum" in exc.value.detail


def test_delete_is_recoverable_and_purge_is_not(db, user, taxable, scenario):
    """Deleting parks a scenario in the retention window: it disappears from
    the active list and stops running, but its data is still there until the
    window closes or the user purges it."""
    _buy(db, taxable)
    db.commit()
    doomed = scenarios.create(db, user, "Doomed", copy_from_id=scenario.id)
    db.commit()
    doomed_accounts = [a.id for a in db.query(Account).filter_by(scenario_id=doomed.id).all()]
    assert doomed_accounts

    scenarios.delete(db, user, doomed)
    db.commit()

    assert doomed.deleted_at is not None
    assert [s.id for s in scenarios.list_for(db, user)] == [scenario.id]
    assert [s.id for s in scenarios.list_deleted(db, user)] == [doomed.id]
    # the data is still there — that is what makes it recoverable
    assert db.query(Account).filter(Account.id.in_(doomed_accounts)).count() == len(doomed_accounts)
    # and it is frozen: its accounts are excluded from background work
    frozen = {row[0] for row in db.execute(scenarios.frozen_accounts(db)).all()}
    assert frozen == set(doomed_accounts)

    restored = scenarios.restore(db, user, doomed)
    db.commit()
    assert restored.deleted_at is None
    assert len(scenarios.list_for(db, user)) == 2
    assert db.execute(scenarios.frozen_accounts(db)).first() is None

    # purging is the destructive one, and only after a delete
    with pytest.raises(HTTPException) as exc:
        scenarios.purge(db, user, restored)
    assert exc.value.status_code == 422

    scenarios.delete(db, user, restored)
    scenarios.purge(db, user, restored)
    db.commit()
    assert db.query(Account).filter(Account.id.in_(doomed_accounts)).count() == 0
    assert db.query(Transaction).filter(Transaction.account_id.in_(doomed_accounts)).count() == 0
    # the original track is untouched throughout
    assert db.query(Account).filter_by(scenario_id=scenario.id).count() == 1
    assert metrics.summary(db, user, None, scenario.id).invested_value > 0


def test_expired_scenarios_are_purged_after_the_window(db, user, taxable, scenario):
    from datetime import datetime, timezone

    from app.models import Scenario as ScenarioModel

    fresh = scenarios.create(db, user, "Yesterday", copy_from_id=scenario.id)
    old = scenarios.create(db, user, "Long gone", copy_from_id=scenario.id)
    db.commit()
    scenarios.delete(db, user, fresh)
    scenarios.delete(db, user, old)
    old.deleted_at = datetime.now(timezone.utc) - timedelta(days=scenarios.retention_days() + 1)
    db.commit()

    assert scenarios.purge_expired(db) == 1
    db.commit()
    remaining = {s.id for s in db.query(ScenarioModel).all()}
    assert old.id not in remaining and fresh.id in remaining


def test_purge_all_clears_the_deleted_list(db, user, scenario):
    for name in ("One", "Two"):
        s = scenarios.create(db, user, name)
        db.commit()
        scenarios.delete(db, user, s)
    db.commit()
    assert len(scenarios.list_deleted(db, user)) == 2

    assert scenarios.purge_all(db, user) == 2
    db.commit()
    assert scenarios.list_deleted(db, user) == []
    assert [s.id for s in scenarios.list_for(db, user)] == [scenario.id]


def test_a_deleted_scenario_cannot_be_worked_in(db, user, scenario):
    from starlette.datastructures import Headers, QueryParams

    from app.deps import resolve_scenario

    other = scenarios.create(db, user, "Gone")
    db.commit()
    scenarios.delete(db, user, other)
    db.commit()

    class Req:
        headers = Headers({})
        query_params = QueryParams({})

    with pytest.raises(HTTPException) as exc:
        resolve_scenario(Req(), user, db, header_value=other.id)
    assert exc.value.status_code == 404
    # and the default falls back to a live one
    assert resolve_scenario(Req(), user, db).id == scenario.id


def test_deleting_the_active_default_moves_the_default(db, user, scenario):
    other = scenarios.create(db, user, "Second")
    db.commit()
    user.default_scenario_id = other.id
    db.commit()

    scenarios.delete(db, user, other)
    db.commit()
    assert user.default_scenario_id == scenario.id


def test_purge_all_route_is_not_swallowed_by_the_id_route(db, user, scenario):
    """`/scenarios/deleted/purge` and `/scenarios/{id}/purge` are the same
    shape; the literal one has to be declared first or "deleted" is read as a
    scenario id and the bulk purge 404s."""
    from app.routers.scenarios import router

    paths = [r.path for r in router.routes]
    literal = paths.index("/scenarios/deleted/purge")
    parameterised = paths.index("/scenarios/{scenario_id}/purge")
    assert literal < parameterised
    assert paths.index("/scenarios/deleted") < parameterised
    assert paths.index("/scenarios/import") < paths.index("/scenarios/{scenario_id}")
