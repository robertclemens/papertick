"""The price-convention probe: measured, not assumed.

Pinning `adjustment=split` is a promise the vendor makes. These verify the
check that turns it into a fact, and that the check itself discriminates.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services import convention
from app.services.convention import FIXTURES, REQUIRED, matches, probe_provider


class _Stub:
    """Replays a fixture window at a chosen convention."""

    has_dividends = False

    def __init__(self, name, which, bad_ticker=None):
        self.name = name
        self.which = which
        self.bad_ticker = bad_ticker

    def history(self, ticker, start, end):
        f = next(f for f in FIXTURES if f.ticker == ticker)
        which = self.which
        if self.bad_ticker and ticker != self.bad_ticker:
            which = REQUIRED
        base = Decimal("100")
        return [(f.start, base), (f.end, base * Decimal(str(f.expected[which])))]


def test_probe_passes_a_correctly_adjusted_provider():
    v = probe_provider(_Stub("good", "split"))
    assert v.status == "ok" and v.usable


@pytest.mark.parametrize("wrong", ["total_return", "raw"])
def test_probe_fails_a_provider_on_any_other_convention(wrong):
    """Both wrong conventions must be caught, not just the total-return one.

    `total_return` double-counts dividends; `raw` leaves splits unrestated. A
    ledger built on either is wrong in a way no balance check would reveal."""
    v = probe_provider(_Stub("bad", wrong))
    assert v.status == "wrong"
    assert wrong in v.detail
    assert not v.usable


def test_a_symbol_that_never_split_cannot_fail_a_correct_provider():
    """VWELX has no splits, so its raw and split ratios are the same number.

    Treating conventions as mutually exclusive made the real Yahoo provider
    report as 'raw' — the tie resolved by dict order. Matching the required
    convention is a pass whatever else it also matches."""
    vwelx = next(f for f in FIXTURES if f.ticker == "VWELX")
    assert vwelx.expected["raw"] == vwelx.expected["split"], "fixture premise"
    assert set(matches(vwelx.expected["split"], vwelx)) >= {"raw", "split"}
    assert probe_provider(_Stub("good", "split")).status == "ok"


def test_the_split_fixture_is_what_catches_a_raw_feed():
    """A provider that is raw only on the split-bearing symbol is still caught,
    which is why one fixture is not enough."""
    v = probe_provider(_Stub("mixed", "raw", bad_ticker="AAPL"))
    assert v.status == "wrong" and "AAPL" in v.detail


def test_fixtures_can_actually_discriminate():
    """A fixture whose conventions are within tolerance of each other proves
    nothing. AAPL must separate all three; VWELX must separate the dividend
    adjustment."""
    aapl = next(f for f in FIXTURES if f.ticker == "AAPL")
    vals = aapl.expected
    for a, b in [("raw", "split"), ("split", "total_return"), ("raw", "total_return")]:
        gap = abs(vals[a] - vals[b]) / vals[b]
        assert gap > convention.TOLERANCE * 2, f"AAPL cannot separate {a} from {b}"
    vwelx = next(f for f in FIXTURES if f.ticker == "VWELX")
    gap = abs(vwelx.expected["split"] - vwelx.expected["total_return"]) / vwelx.expected["split"]
    assert gap > convention.TOLERANCE * 2


def test_unreachable_provider_is_unknown_not_wrong():
    """A provider that is down is not a provider that is lying. Quarantining
    on a failed fetch would turn a network blip into an outage."""
    class Dead:
        name = "dead"
        def history(self, *a, **k):
            raise RuntimeError("connection refused")

    v = probe_provider(Dead())
    assert v.status == "unknown" and v.usable


def test_quarantine_only_acts_on_a_proven_mismatch(monkeypatch):
    from app.config import get_settings

    store = {}

    class _Fake:
        def hset(self, k, mapping=None): store[k] = dict(mapping)
        def hgetall(self, k): return store.get(k, {})
        def expire(self, k, s): pass

    monkeypatch.setattr(convention, "get_redis", lambda: _Fake())
    monkeypatch.setattr(get_settings(), "convention_quarantine", True, raising=False)

    convention.record(probe_provider(_Stub("good", "split")))
    assert not convention.quarantined("good")

    convention.record(probe_provider(_Stub("bad", "total_return")))
    assert convention.quarantined("bad")

    class Dead:
        name = "dead"
        def history(self, *a, **k): raise RuntimeError("down")
    convention.record(probe_provider(Dead()))
    assert not convention.quarantined("dead"), "a down provider must not be quarantined"

    # never checked at all
    assert not convention.quarantined("never-seen")


# --------------------------------------------------- write-path freshness gate

def test_a_fresh_verdict_costs_nothing(monkeypatch):
    """Enforcement runs on every fill, so it has to be a cache read, not a
    network call. Only re-measuring may reach a provider."""
    from datetime import datetime, timedelta, timezone

    from app.config import get_settings

    store, probes = {}, {"n": 0}

    class _Fake:
        def hset(self, k, mapping=None): store[k] = dict(mapping)
        def hgetall(self, k): return store.get(k, {})
        def expire(self, k, s): pass
        def set(self, k, v, nx=False, ex=None): return True
        def delete(self, k): store.pop(k, None)

    monkeypatch.setattr(convention, "get_redis", lambda: _Fake())
    monkeypatch.setattr(get_settings(), "convention_max_age_hours", 24, raising=False)
    monkeypatch.setattr(get_settings(), "convention_probe", True, raising=False)

    class Counting(_Stub):
        def history(self, ticker, start, end):
            probes["n"] += 1
            return _Stub.history(self, ticker, start, end)

    provider = Counting("p", "split")
    convention.ensure_fresh([provider])
    first = probes["n"]
    assert first > 0, "an unverified provider must be measured"

    for _ in range(20):
        convention.ensure_fresh([provider])
    assert probes["n"] == first, "a fresh verdict must not re-measure"


def test_a_stale_verdict_is_re_measured(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.config import get_settings

    store, probes = {}, {"n": 0}

    class _Fake:
        def hset(self, k, mapping=None): store[k] = dict(mapping)
        def hgetall(self, k): return store.get(k, {})
        def expire(self, k, s): pass
        def set(self, k, v, nx=False, ex=None): return True
        def delete(self, k): store.pop(k, None)

    monkeypatch.setattr(convention, "get_redis", lambda: _Fake())
    monkeypatch.setattr(get_settings(), "convention_probe", True, raising=False)
    monkeypatch.setattr(get_settings(), "convention_max_age_hours", 24, raising=False)

    class Counting(_Stub):
        def history(self, ticker, start, end):
            probes["n"] += 1
            return _Stub.history(self, ticker, start, end)

    provider = Counting("p", "split")
    convention.ensure_fresh([provider])
    before = probes["n"]

    # age the stored verdict past the window
    key = convention.VERDICT_KEY.format("p")
    store[key]["checked_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=30)
    ).isoformat()
    convention.ensure_fresh([provider])
    assert probes["n"] > before, "a stale verdict must be re-measured before a fill"


def test_a_broken_probe_never_blocks_a_trade(monkeypatch):
    """Verification is a guard, not a gate. If the check itself fails, the
    existing verdicts still govern the chain — failing the trade would be the
    worse trade-off."""
    from app.services import convention as c

    def boom(*a, **k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(c, "ensure_fresh", boom)
    c.ensure_fresh_for_write()      # must not raise


def test_no_scheduled_task_probes_conventions_on_a_clock():
    """An idle deployment must not reach a provider to re-verify something
    nothing is about to use."""
    from app.workers.celery_app import celery

    tasks = {e["task"] for e in celery.conf.beat_schedule.values()}
    assert "app.workers.tasks.verify_price_conventions" not in tasks


def test_a_truncated_window_is_unmeasurable_not_a_verdict():
    """A provider that silently shortens the window returns a real ratio over
    the wrong period. Comparing that to the fixture's references is how a
    probe hands out a confident, wrong answer — Nasdaq caps history at about
    seven years and ignores `fromdate` beyond it."""
    from datetime import timedelta
    from decimal import Decimal

    fixture = FIXTURES[0]

    class Truncated:
        name = "truncated"
        def history(self, ticker, start, end):
            f = next(x for x in FIXTURES if x.ticker == ticker)
            late = f.end - timedelta(days=400)          # only the tail
            return [(late, Decimal("100")), (f.end, Decimal("120"))]

    v = probe_provider(Truncated())
    assert v.status == "unknown", "a truncated window must not produce a verdict"
    assert "cannot rule out" in v.detail
    assert "skipped" in v.detail, "the reason each fixture was unusable must be reported"
    assert v.usable, "unmeasurable is not the same as wrong"


# ------------------------------------------------------- independent oracle

def test_oracle_never_compares_two_different_days(monkeypatch):
    """The cross-check must compare like with like.

    `close_on` falls back to the nearest earlier session, so using it here
    turns two correct numbers into a manufactured disagreement — measured at
    0.21% on VWELX before this was fixed. A cross-check that cries wolf is
    worse than none, because it teaches you to ignore it."""
    from datetime import date
    from decimal import Decimal

    from app.services import oracle
    from app.services.market_data import market_data

    day = date(2026, 8, 28)
    # our provider has not printed `day` yet; it only has the day before
    monkeypatch.setattr(market_data, "close_exact", lambda t, d: None)
    monkeypatch.setattr(market_data, "close_on",
                        lambda t, d: Decimal("47.43"))   # the *earlier* close
    monkeypatch.setattr(oracle, "_nasdaq_closes",
                        lambda t, s, e: {day: Decimal("47.33")})

    c = oracle.compare_close("VWELX", day)
    assert c.verdict == "unavailable", "a missing day must not be reported as a difference"
    assert c.difference_pct is None


def test_oracle_reports_agreement_on_a_matched_day(monkeypatch):
    from datetime import date
    from decimal import Decimal

    from app.services import oracle
    from app.services.market_data import market_data

    day = date(2026, 8, 27)
    monkeypatch.setattr(market_data, "close_exact", lambda t, d: Decimal("47.43"))
    monkeypatch.setattr(oracle, "_nasdaq_closes", lambda t, s, e: {day: Decimal("47.43")})
    c = oracle.compare_close("VWELX", day)
    assert c.verdict == "agree" and c.difference_pct == 0


def test_oracle_refuses_dates_beyond_the_reference_history(monkeypatch):
    """The reference truncates at about seven years and answers anyway, so the
    limit has to be enforced on our side."""
    from datetime import date

    from app.services import oracle

    c = oracle.compare_close("FCNTX", date(2015, 1, 2))
    assert c.verdict == "unavailable" and "years of history" in c.note



def test_passing_only_the_fixtures_you_can_reach_is_not_proof():
    """Coverage is part of the verdict.

    A provider whose history is too short to reach any fixture that separates
    dividend adjustment has not been shown to be on the right convention — it
    has been shown not to contradict the fixtures it happened to cover. Reading
    that as a pass turns a blind spot into a guarantee."""
    from decimal import Decimal

    # only the NVDA window, which rules out `raw` but says nothing about
    # dividend adjustment (its split and total-return ratios sit 0.25% apart)
    nvda = next(f for f in FIXTURES if f.ticker == "NVDA")
    assert nvda.discriminates() == frozenset({"raw"})

    class ShortHistory:
        name = "short"
        def history(self, ticker, start, end):
            if ticker != "NVDA":
                raise RuntimeError("no history that far back")
            f = nvda
            base = Decimal("100")
            return [(f.start, base),
                    (f.end, base * Decimal(str(f.expected[REQUIRED])))]

    v = probe_provider(ShortHistory())
    assert v.status == "unknown"
    assert "total_return" in v.detail
    assert v.usable


def test_the_fixture_set_can_discriminate_every_wrong_convention():
    """Across the whole set, each wrong convention must be catchable by at
    least one fixture — otherwise no provider could ever be verified."""
    covered = set()
    for f in FIXTURES:
        covered |= f.discriminates()
    assert {"raw", "total_return"} <= covered
