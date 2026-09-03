"""The published-limit refresh: history, deadlines, parsers, reconciliation.

Every test here is offline. The parsers are fed captured markup rather than
irs.gov, so the suite does not depend on a government website being up (or on
its content this week), and `fetch_official_limits` is only exercised through
its failure path.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.init_db import IRS_LIMITS
from app.models import IrsLimit, IrsLimitCheck, LimitCheckOutcome
from app.services import irs, irs_source


# ------------------------------------------------------------------- history

def test_history_starts_at_1997_with_no_gaps():
    years = [row[0] for row in IRS_LIMITS]
    assert years[0] == 1997
    assert years == sorted(years), "seed table must stay in year order"
    assert years == list(range(years[0], years[-1] + 1)), "no missing tax years"


@pytest.mark.parametrize("year,limit,catchup", [
    (1997, "2000", "0"),      # pre-EGTRRA: no catch-up existed
    (2001, "2000", "0"),
    (2002, "3000", "500"),    # EGTRRA introduces the catch-up at $500
    (2005, "4000", "500"),
    (2006, "4000", "1000"),   # catch-up steps to $1,000
    (2008, "5000", "1000"),
    (2013, "5500", "1000"),
    (2019, "6000", "1000"),
    (2024, "7000", "1000"),
    (2026, "7500", "1100"),   # SECURE 2.0 indexing lifts the catch-up
])
def test_known_published_figures(year, limit, catchup):
    row = next(r for r in IRS_LIMITS if r[0] == year)
    assert row[1] == limit
    assert row[2] == catchup


def test_pre_2002_catchup_is_zero_not_inherited():
    """A naive carry-forward would hand 1997-2001 the $1,000 catch-up that did
    not exist yet, quietly raising those years' limit by half."""
    assert all(r[2] == "0" for r in IRS_LIMITS if r[0] < 2002)
    assert all(r[2] != "0" for r in IRS_LIMITS if r[0] >= 2002)


# ------------------------------------------------------------------ tax day

def test_tax_day_reproduces_every_seeded_deadline():
    """The seeded deadlines were entered by hand; `tax_day` must derive the
    same dates, or a projected year gets a deadline the real one never had.
    The 2019 and 2020 COVID postponements are statutory one-offs and are not
    derivable, so they are excluded."""
    covid = {2019, 2020}
    for year, _limit, _catchup, deadline in IRS_LIMITS:
        if year in covid:
            continue
        assert irs.tax_day(year + 1) == deadline, f"tax year {year}"


@pytest.mark.parametrize("filing_year,expected", [
    (2000, date(2000, 4, 17)),  # Apr 15 Saturday -> Monday
    (2001, date(2001, 4, 16)),  # Apr 15 Sunday -> Monday
    (2006, date(2006, 4, 17)),  # weekend only; Emancipation Day not yet in play
    (2011, date(2011, 4, 18)),  # Emancipation Day observed Fri Apr 15
    (2017, date(2017, 4, 18)),  # weekend then Emancipation Day Mon Apr 17
    (2025, date(2025, 4, 15)),  # ordinary Tuesday
])
def test_tax_day_edge_cases(filing_year, expected):
    assert irs.tax_day(filing_year) == expected


def test_emancipation_day_not_applied_before_2007():
    """DC made Emancipation Day a holiday in 2005, but 2007 was the first
    filing season it moved the federal deadline. Applying it to 2006 would
    date that year a day late."""
    assert irs.tax_day(2006) == date(2006, 4, 17)


# ------------------------------------------------------------------ parsers

COLA_PAGE = """
<h1>COLA increases</h1>
<table>
  <tr><th>IRAs</th><th>2026</th><th>2025</th><th>2024</th><th>2023</th></tr>
  <tr><td>IRA contribution limit</td><td>$7,500</td><td>$7,000</td>
      <td>$7,000</td><td>$6,500</td></tr>
  <tr><td>IRA catch-up contributions</td><td>1,100</td><td>1,000</td>
      <td>1,000</td><td>1,000</td></tr>
</table>
<table>
  <tr><th>Traditional IRA AGI deduction phase-out starting at</th><th>2026</th></tr>
  <tr><td>Joint return</td><td>129,000</td></tr>
</table>
"""

PUB590A_PAGE = """
<p>you can contribute to a traditional IRA up to: $7,000, or $8,000 if you
were age 50 or older by the end of 2025.</p>
"""


def test_parse_cola_reads_the_ira_table():
    got = irs_source.parse_cola(COLA_PAGE, today=date(2026, 9, 3))
    assert sorted(got) == [2023, 2024, 2025, 2026]
    assert got[2026].ira_limit == Decimal("7500")
    assert got[2026].ira_catchup == Decimal("1100")
    assert got[2023].ira_limit == Decimal("6500")
    assert got[2026].source_name == "cola"


def test_parse_cola_ignores_the_phase_out_table():
    """The phase-out table sits on the same page with the same year header.
    Its $129,000 must never be read as a contribution limit."""
    got = irs_source.parse_cola(COLA_PAGE, today=date(2026, 9, 3))
    assert all(f.ira_limit < Decimal("100000") for f in got.values())
    assert Decimal("129000") not in {f.ira_limit for f in got.values()}


def test_parse_pub590a_splits_the_combined_figure():
    """590-A quotes the limit and the limit-plus-catch-up; the catch-up is
    their difference, not the second number."""
    got = irs_source.parse_pub590a(PUB590A_PAGE, today=date(2026, 9, 3))
    assert got[2025].ira_limit == Decimal("7000")
    assert got[2025].ira_catchup == Decimal("1000")


def test_parser_drops_implausible_figures():
    """A markup change that shifts the parser onto the wrong cell must yield
    nothing rather than a confident wrong number."""
    bad = COLA_PAGE.replace("$7,500", "$412,345")
    got = irs_source.parse_cola(bad, today=date(2026, 9, 3))
    assert 2026 not in got
    assert 2025 in got, "one bad cell must not discard the whole table"


def test_parser_rejects_non_multiples_of_the_indexing_step():
    bad = COLA_PAGE.replace("$7,000", "$7,123")
    got = irs_source.parse_cola(bad, today=date(2026, 9, 3))
    assert 2025 not in got and 2024 not in got


def test_parser_rejects_years_beyond_the_horizon():
    far = COLA_PAGE.replace("2026", "2099")
    got = irs_source.parse_cola(far, today=date(2026, 9, 3))
    assert 2099 not in got


def test_fetch_raises_when_every_source_fails(monkeypatch):
    """An outage must surface as an error so the task retries — never as
    silent agreement that the stored limits are correct."""
    monkeypatch.setattr(irs_source, "_fetch",
                        lambda url: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(irs_source, "SOURCES", [
        ("cola", "u1", irs_source.parse_cola),
        ("pub590a", "u2", irs_source.parse_pub590a),
    ])
    with pytest.raises(OSError):
        irs_source.fetch_official_limits(today=date(2026, 9, 3))


def test_falls_back_to_second_source(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == irs_source.COLA_URL:
            return "<html>no table here</html>"
        return PUB590A_PAGE

    monkeypatch.setattr(irs_source, "_fetch", fake_fetch)
    got = irs_source.fetch_official_limits(today=date(2026, 9, 3))
    assert calls == [irs_source.COLA_URL, irs_source.PUB590A_URL]
    assert got[2025].source_name == "pub590a"


# ----------------------------------------------------------- reconciliation

def _fetched(year, limit, catchup, name="cola"):
    return {year: irs_source.FetchedLimit(
        year, Decimal(limit), Decimal(catchup), name, "https://irs.gov/x")}


def test_projection_is_replaced_by_official_figures(db):
    db.add(IrsLimit(tax_year=2027, ira_limit=Decimal("7500"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2028, 4, 15), source="projected"))
    db.commit()

    checks = irs_source.reconcile_limits(
        db, today=date(2027, 1, 5), fetched=_fetched(2027, "8000", "1100"))

    row = db.get(IrsLimit, 2027)
    assert row.ira_limit == Decimal("8000")
    assert row.source == "official"
    assert row.verified_at is not None
    assert row.designation_deadline == irs.tax_day(2028)
    assert [c.outcome for c in checks] == [LimitCheckOutcome.UPDATED]


def test_official_row_is_never_overwritten_on_mismatch(db):
    db.add(IrsLimit(tax_year=2026, ira_limit=Decimal("7500"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2027, 4, 15), source="official"))
    db.commit()

    checks = irs_source.reconcile_limits(
        db, today=date(2026, 9, 3), fetched=_fetched(2026, "9500", "1100"))

    row = db.get(IrsLimit, 2026)
    assert row.ira_limit == Decimal("7500"), "hand-checked figure must stand"
    assert row.source == "official"
    assert [c.outcome for c in checks] == [LimitCheckOutcome.MISMATCH]
    assert checks[0].found_limit == Decimal("9500")
    assert "stored value kept" in checks[0].detail


def test_agreement_stamps_verification(db):
    db.add(IrsLimit(tax_year=2026, ira_limit=Decimal("7500"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2027, 4, 15), source="official"))
    db.commit()

    checks = irs_source.reconcile_limits(
        db, today=date(2026, 9, 3), fetched=_fetched(2026, "7500", "1100"))

    row = db.get(IrsLimit, 2026)
    assert row.verified_at is not None
    assert row.verified_from == "https://irs.gov/x"
    assert [c.outcome for c in checks] == [LimitCheckOutcome.CONFIRMED]


def test_missing_year_is_added(db):
    checks = irs_source.reconcile_limits(
        db, today=date(2026, 9, 3), fetched=_fetched(2027, "8000", "1200"))

    row = db.get(IrsLimit, 2027)
    assert row is not None
    assert row.ira_limit == Decimal("8000")
    assert row.source == "official"
    assert row.designation_deadline == irs.tax_day(2028)
    assert [c.outcome for c in checks] == [LimitCheckOutcome.ADDED]


def test_check_rows_are_persisted_for_audit(db):
    irs_source.reconcile_limits(
        db, today=date(2026, 9, 3), fetched=_fetched(2027, "8000", "1200"))
    stored = db.query(IrsLimitCheck).all()
    assert len(stored) == 1
    assert stored[0].tax_year == 2027
    assert stored[0].source_url == "https://irs.gov/x"


# ---------------------------------------------- history feeds the rule engine

def test_pre_2002_catchup_age_does_not_inflate_the_limit(db, user):
    """A 55-year-old contributing for 1999 gets $2,000 — not $3,000 — and is
    not told their limit includes a catch-up."""
    db.add(IrsLimit(tax_year=1999, ira_limit=Decimal("2000"),
                    ira_catchup=Decimal("0"),
                    designation_deadline=date(2000, 4, 17)))
    user.date_of_birth = date(1944, 6, 1)  # turns 55 in 1999
    db.commit()

    limit, catchup = irs.user_limit(db, user, 1999)
    assert limit == Decimal("2000")
    assert catchup is False


def test_2005_catchup_is_500_not_1000(db, user):
    db.add(IrsLimit(tax_year=2005, ira_limit=Decimal("4000"),
                    ira_catchup=Decimal("500"),
                    designation_deadline=date(2006, 4, 17)))
    user.date_of_birth = date(1950, 6, 1)  # turns 55 in 2005
    db.commit()

    limit, catchup = irs.user_limit(db, user, 2005)
    assert limit == Decimal("4500")
    assert catchup is True


# ------------------------------------------------- when the refresh runs at all

@pytest.mark.parametrize("today,expected", [
    (date(2026, 1, 15), 2026),   # before the release window: this year is newest
    (date(2026, 9, 3), 2026),
    (date(2026, 10, 1), 2027),   # October: next year becomes knowable
    (date(2026, 11, 20), 2027),
    (date(2026, 12, 31), 2027),
])
def test_latest_publishable_year(today, expected):
    assert irs_source.latest_publishable_year(today) == expected


def test_refresh_needed_while_next_year_is_projected(db):
    db.add(IrsLimit(tax_year=2027, ira_limit=Decimal("7500"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2028, 4, 18), source="projected"))
    db.commit()
    needed, reason = irs_source.refresh_needed(db, today=date(2026, 11, 2))
    assert needed is True
    assert "2027" in reason


def test_refresh_stops_once_the_year_is_official(db):
    """The stop condition: nothing left for a fetch to settle, so the weekly
    beat stops making requests even though it keeps ticking."""
    db.add(IrsLimit(tax_year=2027, ira_limit=Decimal("8000"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2028, 4, 18), source="official"))
    db.commit()
    needed, reason = irs_source.refresh_needed(db, today=date(2026, 11, 30))
    assert needed is False
    assert "nothing projected" in reason


def test_unpublishable_future_projection_does_not_trigger_a_fetch(db):
    """`ensure_limits` always keeps a projection for the year after next. The
    IRS cannot have published it yet, so treating it as work to do would make
    the job fetch forever and never stop."""
    db.add(IrsLimit(tax_year=2027, ira_limit=Decimal("8000"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2028, 4, 18), source="official"))
    db.add(IrsLimit(tax_year=2028, ira_limit=Decimal("8000"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2029, 4, 17), source="projected"))
    db.commit()
    needed, _ = irs_source.refresh_needed(db, today=date(2027, 1, 11))
    assert needed is False, "2028 is not knowable in January 2027"

    # ...but once October 2027 arrives it is, and the job wakes up for it.
    needed, _ = irs_source.refresh_needed(db, today=date(2027, 11, 1))
    assert needed is True


def test_beat_window_is_november_through_january():
    from app.workers.celery_app import celery

    sched = celery.conf.beat_schedule["refresh-irs-limits"]["schedule"]
    assert sorted(sched.month_of_year) == [1, 11, 12]
    assert sorted(sched.day_of_week) == [1], "Mondays only"


def test_stale_current_year_projection_is_warned_about(db, caplog):
    """A deployment that missed the Nov-Jan window enforces a guessed limit all
    year; that must not be silent."""
    db.add(IrsLimit(tax_year=2025, ira_limit=Decimal("7000"),
                    ira_catchup=Decimal("1000"),
                    designation_deadline=date(2026, 4, 15), source="official"))
    db.add(IrsLimit(tax_year=2026, ira_limit=Decimal("7000"),
                    ira_catchup=Decimal("1000"),
                    designation_deadline=date(2027, 4, 15), source="projected"))
    db.commit()

    with caplog.at_level("WARNING"):
        irs.ensure_limits(db, today=date(2026, 6, 1))
    assert any("still a projection" in r.message for r in caplog.records)


def test_official_current_year_warns_about_nothing(db, caplog):
    db.add(IrsLimit(tax_year=2026, ira_limit=Decimal("7500"),
                    ira_catchup=Decimal("1100"),
                    designation_deadline=date(2027, 4, 15), source="official"))
    db.commit()
    with caplog.at_level("WARNING"):
        irs.ensure_limits(db, today=date(2026, 6, 1))
    assert not any("still a projection" in r.message for r in caplog.records)
