"""Verify — from data — how each provider adjusts its historical prices.

Pinning a query parameter is only half the job. `adjustment=split` is a
promise the vendor makes, not a fact we have checked, and vendors change
defaults, rename options and quietly restate series. A ledger built on the
wrong convention is not visibly broken: every number still looks like a price.

So each provider is measured against fixtures whose answers are permanent
historical fact, and a provider that has drifted is taken out of the chain
rather than allowed to write invented cost basis into the ledger.

Three conventions exist in the wild, and all three were observed while
building this:

  raw            the price as printed that day, never restated.
                 Nasdaq's public API does this: FCNTX on 2015-01-02 comes
                 back as $97.80.
  split          restated for later splits only. The same day, Yahoo's
                 `quote.close` says $9.78 — FCNTX split 10:1 in 2018.
                 THIS is the convention PaperTick requires.
  total_return   restated for splits *and* distributions, so every past price
                 is marked down by the dividends paid since. Yahoo's
                 `adjclose` says $4.80 for that day.

Why ratios rather than prices: a fixture pinned to an absolute price breaks
the next time the security splits, producing a false alarm forever after. A
ratio between two dates that are both already in the past is stable — a
split-adjusted series restates both endpoints equally, so the ratio it implies
never moves, and the raw and total-return ratios are fixed historical facts.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import redis

from app.config import get_settings
from app.rate_limit import get_redis

log = logging.getLogger("papertick.convention")

REQUIRED = "split"
VERDICT_KEY = "md:convention:{}"
# Conventions sit 10%-300% apart; vendors disagree on a closing print by a few
# basis points at most. 2% separates them without flagging honest noise.
TOLERANCE = 0.02
# How far an endpoint bar may sit from the fixture date. Holidays and thin
# trading move it by a day or two; anything more means the provider is not
# answering the question that was asked.
ENDPOINT_SLACK_DAYS = 10


@dataclass(frozen=True)
class Fixture:
    """A window whose answer is a matter of record, not of opinion."""

    ticker: str
    start: date
    end: date
    expected: dict[str, float]
    note: str

    def discriminates(self) -> frozenset[str]:
        """Which wrong conventions this fixture can actually rule out.

        A fixture only proves something where the conventions it is comparing
        are further apart than the tolerance. NVDA over 2021-2025 separates
        `raw` from `split` by a factor of ten, but its split and total-return
        ratios sit 0.25% apart — so it says nothing about dividend adjustment,
        and pretending otherwise would turn a blind spot into a pass.
        """
        target = self.expected[REQUIRED]
        return frozenset(
            name for name, value in self.expected.items()
            if name != REQUIRED and abs(value - target) / target > TOLERANCE * 2
        )


# Two fixtures are the minimum, and neither alone is sufficient:
#   * a symbol with no splits cannot tell `raw` from `split` — measured
#     against Nasdaq, VWELX and VTSAX matched Yahoo to 0.0000% while FCNTX
#     was out by exactly 10x;
#   * a symbol with no meaningful distributions cannot tell `split` from
#     `total_return`.
FIXTURES = (
    Fixture(
        ticker="AAPL", start=date(2015, 1, 2), end=date(2021, 1, 4),
        expected={"raw": 1.183664, "split": 4.734657, "total_return": 5.197489},
        note="spans the 2020 4:1 split — separates raw from adjusted",
    ),
    Fixture(
        ticker="VWELX", start=date(2015, 1, 2), end=date(2021, 1, 4),
        expected={"raw": 1.122829, "split": 1.122829, "total_return": 1.651717},
        note="no splits, heavy distributions — separates split from total return",
    ),
    # A second, shallower pair. Some providers simply do not carry a decade:
    # Nasdaq's free endpoint reaches back about ten years, so the 2015 windows
    # above are unmeasurable against it. Without these, such a provider could
    # never be verified at all — and an unverifiable provider that is still
    # used is the exact gap this module exists to close.
    Fixture(
        ticker="NVDA", start=date(2021, 1, 4), end=date(2025, 1, 2),
        expected={"raw": 0.263679, "split": 10.547146, "total_return": 10.573913},
        note="spans the 2021 4:1 and 2024 10:1 splits — separates raw from adjusted",
    ),
    Fixture(
        ticker="VWELX", start=date(2021, 1, 4), end=date(2025, 1, 2),
        expected={"raw": 0.973846, "split": 0.973846, "total_return": 1.347879},
        note="no splits, heavy distributions — separates split from total return",
    ),
    # An *equity* with the same job as VWELX. A provider that serves only
    # equities — Nasdaq's free endpoint is deliberately used that way — can
    # reach no fund fixture at all, so without this it could never be shown to
    # be free of dividend adjustment and would sit permanently unverified.
    # Altria pays enough that four years separate the two conventions by 38%.
    Fixture(
        ticker="MO", start=date(2021, 1, 4), end=date(2025, 1, 2),
        expected={"raw": 1.285749, "split": 1.285749, "total_return": 1.771275},
        note="high-yield equity, no splits — separates split from total return",
    ),
)


def matches(observed: float, fixture: Fixture) -> list[str]:
    """Every convention this ratio is consistent with, nearest first.

    Conventions are not mutually exclusive per fixture, and assuming they were
    is a real trap: VWELX has never split, so its `raw` and `split` ratios are
    the same number. A correct provider tested only against VWELX therefore
    "matches raw" as truly as it matches split. The verdict logic below treats
    matching the required convention as a pass whatever else it also matches,
    and only a fixture that can actually discriminate can fail a provider.
    """
    hits = [
        (abs(observed - expected) / expected, name)
        for name, expected in fixture.expected.items()
        if abs(observed - expected) / expected <= TOLERANCE
    ]
    return [name for _, name in sorted(hits)]


@dataclass
class Verdict:
    provider: str
    status: str                  # ok | wrong | unknown
    detail: str
    checked_at: datetime

    @property
    def usable(self) -> bool:
        # `unknown` stays usable: a fetch that failed is a provider being down,
        # not a provider being wrong, and taking pricing offline over a network
        # blip would be its own outage.
        return self.status != "wrong"


def probe_provider(provider) -> Verdict:
    """Measure one provider against every fixture."""
    now = datetime.now(timezone.utc)
    seen: list[str] = []
    ruled_out: set[str] = set()
    skipped: list[str] = []
    for fixture in FIXTURES:
        try:
            candles = provider.history(fixture.ticker, fixture.start, fixture.end)
        except Exception as exc:                                    # noqa: BLE001
            skipped.append(f"{fixture.ticker} {fixture.start.year}: {exc}")
            continue
        by_date = {d: float(p) for d, p in candles}
        if not by_date:
            skipped.append(f"{fixture.ticker} {fixture.start.year}: no data")
            continue
        # Vendors differ on which days they carry, so the endpoints are the
        # closest bars rather than exact dates — but they must actually be
        # close. A provider that silently truncates the window returns a real
        # ratio measured over a *different* period, and comparing that to this
        # fixture's references is how a probe hands out a confident, wrong
        # verdict. Nasdaq caps history at roughly seven years and ignores
        # `fromdate` beyond it, which is exactly this failure.
        try:
            first = min(d for d in by_date if d >= fixture.start)
            last = max(d for d in by_date if d <= fixture.end)
        except ValueError:
            skipped.append(f"{fixture.ticker} {fixture.start.year}: window not covered")
            continue
        if ((first - fixture.start).days > ENDPOINT_SLACK_DAYS
                or (fixture.end - last).days > ENDPOINT_SLACK_DAYS
                or first >= last or not by_date[first]):
            skipped.append(
                f"{fixture.ticker} {fixture.start.year}: only {first}..{last} available")
            continue

        ratio = by_date[last] / by_date[first]
        found = matches(ratio, fixture)
        if REQUIRED in found:
            seen.append(f"{fixture.ticker}/{fixture.start.year}={ratio:.4f}")
            ruled_out |= (fixture.discriminates() - set(found))
            continue
        if found:
            return Verdict(
                provider.name, "wrong",
                f"{fixture.ticker}: ratio {ratio:.6f} is {found[0]!r}, not {REQUIRED!r} "
                f"({fixture.note})", now)
        return Verdict(
            provider.name, "unknown",
            f"{fixture.ticker}: ratio {ratio:.6f} matches no known convention "
            f"(expected {fixture.expected[REQUIRED]:.6f} for {REQUIRED})", now)

    # Passing the fixtures a provider happens to cover is not proof. It is only
    # proof if those fixtures could, between them, have caught each wrong
    # convention — otherwise a blind spot reads as a pass.
    missing = {"raw", "total_return"} - ruled_out
    if missing:
        return Verdict(
            provider.name, "unknown",
            f"cannot rule out {sorted(missing)} — no usable fixture discriminates it"
            + (f" (skipped: {'; '.join(skipped)})" if skipped else ""), now)
    return Verdict(provider.name, "ok", "; ".join(seen), now)


# ------------------------------------------------------------------ storage

def record(verdict: Verdict) -> None:
    try:
        get_redis().hset(VERDICT_KEY.format(verdict.provider), mapping={
            "status": verdict.status,
            "detail": verdict.detail,
            "checked_at": verdict.checked_at.isoformat(),
        })
        # Outlives the probe interval by a wide margin; a verdict that has
        # expired simply means "not yet checked", which is not a failure.
        get_redis().expire(VERDICT_KEY.format(verdict.provider), 120 * 86400)
    except redis.RedisError:
        log.warning("could not store convention verdict for %s", verdict.provider)


def stored(provider_name: str) -> Verdict | None:
    try:
        data = get_redis().hgetall(VERDICT_KEY.format(provider_name))
    except redis.RedisError:
        return None
    if not data:
        return None
    try:
        checked = datetime.fromisoformat(data["checked_at"])
    except (KeyError, ValueError):
        return None
    return Verdict(provider_name, data.get("status", "unknown"),
                   data.get("detail", ""), checked)


def quarantined(provider_name: str) -> bool:
    """Has this provider been measured and found to be on the wrong convention?

    Only a confident `wrong` quarantines. Unproven and unknown both pass: the
    chain must not go dark because a probe has not run yet.
    """
    if not get_settings().convention_quarantine:
        return False
    verdict = stored(provider_name)
    return verdict is not None and verdict.status == "wrong"


def probe_all(providers) -> list[Verdict]:
    """Measure every real provider and persist the verdicts."""
    out: list[Verdict] = []
    for provider in providers:
        verdict = probe_provider(provider)
        record(verdict)
        out.append(verdict)
        if verdict.status == "wrong":
            log.error("market data provider %s is on the WRONG price convention "
                      "and has been quarantined: %s", verdict.provider, verdict.detail)
        elif verdict.status == "unknown":
            log.warning("could not verify price convention for %s: %s",
                        verdict.provider, verdict.detail)
        else:
            log.info("price convention verified for %s (%s)",
                     verdict.provider, verdict.detail)
    return out


# ------------------------------------------------------- write-path freshness

PROBE_LOCK = "md:convention:probing"


def _claim() -> bool:
    """One prober at a time across every process. A caller that loses the race
    proceeds on the existing verdict rather than queueing behind a network
    call — blocking a trade on a lock is worse than the fraction of a second
    of exposure it would avoid."""
    try:
        return bool(get_redis().set(PROBE_LOCK, "1", nx=True, ex=120))
    except redis.RedisError:
        return True


def _release() -> None:
    try:
        get_redis().delete(PROBE_LOCK)
    except redis.RedisError:
        pass


def ensure_fresh(providers) -> None:
    """Re-measure any provider whose verdict is too old to rely on for a write.

    Called on the paths that turn a price into a permanent ledger row, not on
    the paths that merely display one. The asymmetry is the point: a wrong
    price on a dashboard is corrected by the next refresh, a wrong price in a
    fill is corrected by nothing. Displayed *current* prices are unaffected by
    convention in any case — adjustment factors are 1 at the present.

    Cheap by construction: the common case is a cached verdict, which is a
    single Redis read. Only an expired verdict pays for a re-measure, and only
    when something is actually about to trade.
    """
    settings = get_settings()
    if not settings.convention_probe:
        return
    max_age = timedelta(hours=max(0, settings.convention_max_age_hours))
    now = datetime.now(timezone.utc)
    stale = []
    for provider in providers:
        if provider.name == "synthetic":
            continue                      # opt-in sandbox, nothing to verify
        verdict = stored(provider.name)
        if verdict is None or now - verdict.checked_at > max_age:
            stale.append(provider)
    if not stale or not _claim():
        return
    try:
        probe_all(stale)
    finally:
        _release()


def ensure_fresh_for_write() -> None:
    """`ensure_fresh` over whatever the live chain currently is."""
    from app.services.market_data import market_data

    try:
        ensure_fresh(market_data._chain())
    except Exception:                                            # noqa: BLE001
        # Verification is a guard, not a gate: if the probe itself breaks, the
        # existing verdicts still govern the chain and a fill is no worse off
        # than before. Failing the trade here would be the wrong trade-off.
        log.exception("price-convention freshness check failed")
