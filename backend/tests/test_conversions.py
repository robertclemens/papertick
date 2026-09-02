"""Roth conversions and the IRA tax engine.

The rules under test are the ones that quietly produce a wrong number rather
than an error: pro-rata across every pre-tax IRA, the Roth ordering rules, and
the two five-year clocks.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models import (
    Account,
    AccountType,
    CashFlowKind,
    Contribution,
    Conversion,
)
from app.services import conversions, ira


@pytest.fixture()
def traditional(db, user, scenario, voo_asset):
    a = Account(user_id=user.id, scenario_id=scenario.id,
                account_type=AccountType.TRADITIONAL_IRA,
                name="Traditional", settlement_balance=Decimal("20000"))
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def rollover(db, user, scenario):
    a = Account(user_id=user.id, scenario_id=scenario.id,
                account_type=AccountType.ROLLOVER_IRA,
                name="Rollover", settlement_balance=Decimal("30000"))
    db.add(a)
    db.commit()
    return a


# ------------------------------------------------------------------ pro-rata


def test_a_conversion_with_no_basis_is_entirely_taxable(db, user, scenario, traditional, roth):
    plan = conversions.preview(db, user, scenario.id, traditional, roth,
                               amount=Decimal("5000"))
    assert plan["taxable_amount"] == Decimal("5000.00")
    assert plan["nontaxable_amount"] == Decimal("0")


def test_basis_prorates_across_every_pre_tax_ira_not_just_its_own(
    db, user, scenario, traditional, rollover, roth
):
    """The rule that decides whether a backdoor Roth works.

    $5,000 of after-tax basis sits in the Traditional IRA alone, but the split
    is computed against the combined $50,000 of Traditional + Rollover value.
    Converting $5,000 must therefore be 90% taxable — not tax-free, which is
    what someone isolating their basis in one account expects.
    """
    traditional.after_tax_basis = Decimal("5000")
    db.commit()

    plan = conversions.preview(db, user, scenario.id, traditional, roth,
                               amount=Decimal("5000"))
    # 5000 basis / 50000 combined value = 10%
    assert plan["nontaxable_amount"] == Decimal("500.00")
    assert plan["taxable_amount"] == Decimal("4500.00")
    assert plan["total_pre_tax_value"] == Decimal("50000.00")


def test_converting_consumes_the_basis_it_used(db, user, scenario, traditional, rollover, roth):
    traditional.after_tax_basis = Decimal("5000")
    db.commit()
    before = Decimal(traditional.after_tax_basis)

    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("5000"))
    db.commit()

    spent = before - Decimal(traditional.after_tax_basis)
    assert spent == Decimal("500.00"), "only the nontaxable part is basis spent"
    assert Decimal(traditional.settlement_balance) == Decimal("15000.00")
    assert Decimal(roth.settlement_balance) == Decimal("5000.00")


def test_a_conversion_does_not_consume_contribution_room(db, user, scenario, traditional, roth, limits):
    """The single most important rule: conversions have no limit and no cap."""
    from app.services import irs

    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("20000"))
    db.commit()

    contributed = irs.contributed_for_year(db, user, date.today().year, scenario.id)
    assert contributed == Decimal("0"), "a conversion is not a contribution"


# ------------------------------------------------------------------ refusals


def test_the_impossible_directions_are_refused_with_the_reason(
    db, user, scenario, traditional, rollover, roth, taxable
):
    def why(src, dst) -> str:
        with pytest.raises(HTTPException) as exc:
            conversions.assert_convertible(src, dst)
        return exc.value.detail.lower()

    assert "tax cuts and jobs act" in why(roth, traditional)
    assert "contributed" in why(taxable, roth)
    assert "distribution" in why(traditional, taxable)
    assert "transfer" in why(traditional, rollover)

    # and the one that is allowed stays allowed
    conversions.assert_convertible(traditional, roth)
    conversions.assert_convertible(rollover, roth)


# --------------------------------------------------------- Roth ordering rules


def test_roth_ordering_takes_contributions_before_conversions_before_earnings(
    db, user, scenario, traditional, roth
):
    """Contributions come out first, tax and penalty free, whatever your age."""
    db.add(Contribution(account_id=roth.id, tax_year=date.today().year,
                        amount=Decimal("7000"), kind=CashFlowKind.CONTRIBUTION))
    roth.settlement_balance = Decimal("7000")
    db.commit()

    plan = ira.plan_roth_withdrawal(db, user, scenario.id, Decimal("5000"), date.today())
    assert not plan.qualified              # the owner is in their thirties
    assert plan.taxable_income == Decimal("0")
    assert plan.penalty == Decimal("0")
    assert plan.layers[0].label == "Regular contributions"


def test_a_recent_conversion_carries_the_penalty_even_though_it_was_taxed(
    db, user, scenario, traditional, roth
):
    """The per-conversion five-year clock, which is not the account clock.

    The money was already taxed at conversion, so there is no income tax — but
    pulling it out inside five years, under 59½, still costs 10%.
    """
    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("10000"))
    db.commit()

    plan = ira.plan_roth_withdrawal(db, user, scenario.id, Decimal("4000"), date.today())
    assert plan.taxable_income == Decimal("0"), "already taxed at conversion"
    assert plan.penalty == Decimal("400.00"), "10% of the converted amount"
    assert any("less than five years" in n for n in plan.notes)


def test_a_seasoned_conversion_comes_out_clean(db, user, scenario, traditional, roth):
    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("10000"),
                        on=date.today().replace(year=date.today().year - 6))
    db.commit()

    plan = ira.plan_roth_withdrawal(db, user, scenario.id, Decimal("4000"), date.today())
    assert plan.penalty == Decimal("0")
    assert plan.taxable_income == Decimal("0")


def test_earnings_are_ordinary_income_and_penalised(db, user, scenario, roth):
    """Past contributions and conversions, a withdrawal reaches earnings."""
    db.add(Contribution(account_id=roth.id, tax_year=date.today().year,
                        amount=Decimal("1000"), kind=CashFlowKind.CONTRIBUTION))
    roth.settlement_balance = Decimal("3000")
    db.commit()

    plan = ira.plan_roth_withdrawal(db, user, scenario.id, Decimal("3000"), date.today())
    assert plan.taxable_income == Decimal("2000"), "everything past contributions is earnings"
    assert plan.penalty == Decimal("200.00")
    assert plan.layers[-1].label == "Earnings"


def test_a_qualified_distribution_is_clean(db, user, scenario, roth):
    """Both halves of the account clock: five years AND 59½."""
    old = date.today().replace(year=date.today().year - 10)
    db.add(Contribution(account_id=roth.id, tax_year=old.year, amount=Decimal("1000"),
                        kind=CashFlowKind.CONTRIBUTION,
                        timestamp=__import__("app.models", fromlist=["utcnow"]).utcnow()
                        .replace(year=old.year)))
    roth.settlement_balance = Decimal("5000")
    db.commit()

    at_65 = user.date_of_birth + timedelta(days=int(65 * 365.25))
    plan = ira.plan_roth_withdrawal(db, user, scenario.id, Decimal("5000"), at_65)
    assert plan.qualified
    assert plan.taxable_income == Decimal("0") and plan.penalty == Decimal("0")


# ------------------------------------------------- traditional distributions


def test_a_traditional_withdrawal_is_prorata_and_penalised(db, user, scenario, traditional):
    traditional.after_tax_basis = Decimal("2000")   # against 20,000 of value
    db.commit()

    plan = ira.plan_traditional_withdrawal(db, user, scenario.id, Decimal("1000"), date.today())
    assert plan.taxable_income == Decimal("900.00")     # 90% pre-tax
    assert plan.penalty == Decimal("90.00")             # 10% of the taxable part only
    assert plan.basis_used == Decimal("100.00")


def test_an_attested_exception_removes_the_penalty_but_not_the_tax(db, user, scenario, traditional):
    plan = ira.plan_traditional_withdrawal(db, user, scenario.id, Decimal("1000"),
                                           date.today(), penalty_exception=True)
    assert plan.taxable_income == Decimal("1000.00")
    assert plan.penalty == Decimal("0")
    assert any("not verified" in n for n in plan.notes)


def test_after_59_and_a_half_there_is_no_penalty(db, user, scenario, traditional):
    at_60 = user.date_of_birth + timedelta(days=int(60 * 365.25))
    plan = ira.plan_traditional_withdrawal(db, user, scenario.id, Decimal("1000"), at_60)
    assert plan.penalty == Decimal("0")
    assert plan.taxable_income == Decimal("1000.00"), "still ordinary income"


# ------------------------------------------------------------------ in kind


def test_an_in_kind_conversion_moves_shares_at_the_conversion_price(
    db, user, scenario, traditional, roth, voo_asset
):
    from app.models import OrderSource, Position, TaxLot
    from app.schemas import OrderCreateIn
    from app.services import trading

    trading.place_order(
        db, traditional,
        OrderCreateIn(account_id=traditional.id, ticker="VOO", side="BUY",
                      quantity_type="DOLLARS", quantity=Decimal("10000")),
        OrderSource.API,
    )
    db.commit()
    held = Decimal(db.query(Position).filter_by(account_id=traditional.id).one().shares)

    conversions.execute(db, user, scenario.id, traditional, roth,
                        ticker="VOO", shares=held)
    db.commit()

    assert db.query(Position).filter_by(account_id=traditional.id).count() == 0
    arrived = db.query(Position).filter_by(account_id=roth.id).one()
    assert Decimal(arrived.shares) == held
    # the lot arrives at the conversion price, not the original cost
    lot = db.query(TaxLot).filter_by(account_id=roth.id).one()
    assert Decimal(lot.shares_open) == held
    assert lot.acquired_on == date.today()


def test_conversions_survive_an_export_round_trip(db, user, scenario, traditional, roth):
    """A restored Roth has to remember which of its money was converted.

    Lose the conversion rows and the ordering rules reclassify already-taxed
    money as earnings — the restored scenario would tax the same dollars twice
    and penalise them on the way out.
    """
    from app.services import scenarios as svc

    traditional.after_tax_basis = Decimal("4000")
    db.commit()
    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("10000"))
    db.commit()

    payload = svc.export_scenario(db, user, scenario)
    assert len(payload["conversions"]) == 1

    restored = svc.import_scenario(db, user, payload, name="Restored")
    db.commit()

    ids = [a.id for a in db.query(Account).filter_by(scenario_id=restored.id).all()]
    rows = db.query(Conversion).filter(Conversion.to_account_id.in_(ids)).all()
    assert len(rows) == 1
    assert Decimal(rows[0].gross_amount) == Decimal("10000.00")
    assert Decimal(rows[0].taxable_amount) == Decimal("8000.00")   # 4k basis / 20k value
    # and the accounts it names are the restored ones, not the originals
    assert rows[0].from_account_id in ids and rows[0].to_account_id in ids

    # after-tax basis travels too, or the next conversion is priced wrongly
    restored_trad = next(a for a in db.query(Account).filter_by(scenario_id=restored.id)
                         if a.account_type == AccountType.TRADITIONAL_IRA)
    assert Decimal(restored_trad.after_tax_basis) == Decimal(traditional.after_tax_basis)


def test_wiping_a_scenario_removes_its_conversions(db, user, scenario, traditional, roth):
    from app.services import scenarios as svc

    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("5000"))
    db.commit()
    assert db.query(Conversion).count() == 1

    svc.wipe(db, scenario)
    db.commit()
    assert db.query(Conversion).count() == 0, "a wiped scenario must not orphan conversions"


def test_a_statement_covering_a_conversion_renders(db, user, scenario, traditional, roth):
    """A conversion writes a cash-flow kind the statement renderer had never
    seen. An unmapped kind must not take the archive down."""
    from app.models import StatementKind
    from app.services import statements

    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("5000"))
    db.commit()

    today = date.today()
    start = today.replace(day=1)
    data = statements.period_data(db, user, start, today, scenario.id)
    pdf = statements.build_pdf(user, data, StatementKind.MONTHLY, start, today)
    assert pdf.startswith(b"%PDF"), "the statement must still render"


def test_the_tax_report_shows_the_conversion_and_the_basis_left(
    db, user, scenario, traditional, rollover, roth
):
    from app.services import tax

    traditional.after_tax_basis = Decimal("5000")
    db.commit()
    conversions.execute(db, user, scenario.id, traditional, roth, amount=Decimal("10000"))
    db.commit()

    report = tax.tax_report(db, user, date.today().year, scenario_id=scenario.id)
    assert report.conversions == Decimal("10000.00")
    assert report.conversion_taxable == Decimal("9000.00")   # 5k basis / 50k value
    assert report.after_tax_basis == Decimal("4000.00")      # 1k of basis was used
    assert any("no annual limit" in n for n in report.notes)
    assert any("Not modelled" in n for n in report.notes)
