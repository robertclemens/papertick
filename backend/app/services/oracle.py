"""Independent second opinion on a price we already hold.

Nasdaq publishes a free, keyless historical endpoint that covers equities,
ETFs and — unusually — mutual fund NAVs. It cannot be a price source here:

  * history stops at roughly seven years, and `fromdate` beyond that is
    silently ignored, so it cannot price a portfolio whose oldest lot is from
    2012;
  * it publishes RAW prices, unrestated for splits (FCNTX on 2015-01-02 comes
    back as $97.80 against the split-adjusted $9.78), and exposes no splits
    endpoint to restate them with;
  * so a split would have to be inferred from a step in the series, which on
    mutual funds is guessing — a large year-end distribution moves an NAV the
    same way a small split does.

What it is good for is exactly one thing: answering "is the number we are
holding actually right?" for a recent date, from a source that shares no code,
no vendor and no convention with ours. On symbols with no split in the window
it agreed with our prices to 0.0000%.

Deliberately on-demand. Wiring this into a schedule, or into every fill, would
add an upstream request to answer a question nobody asked — the same reasoning
that keeps everything else here demand-driven. It is a diagnostic: reach for it
when a price looks wrong, not on a timer.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx

from app.services.market_data import MarketDataError, market_data

log = logging.getLogger("papertick.oracle")

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept": "application/json"}
_BASE = "https://api.nasdaq.com/api/quote"
# the endpoint is keyed by asset class and gives no way to ask which one a
# symbol is, so they are simply tried in turn
_CLASSES = ("stocks", "etf", "mutualfunds")
# roughly how far back the source carries data before it silently truncates
MAX_LOOKBACK_YEARS = 7
# Two sources pricing the same close should agree to the cent. This allows for
# one taking the closing auction print and the other the last trade.
TOLERANCE_PCT = Decimal("0.5")


@dataclass
class Comparison:
    ticker: str
    on: date
    ours: Decimal | None
    theirs: Decimal | None
    difference_pct: Decimal | None
    verdict: str          # agree | differ | unavailable
    note: str


def _nasdaq_closes(ticker: str, start: date, end: date) -> dict[date, Decimal]:
    for asset_class in _CLASSES:
        try:
            r = httpx.get(f"{_BASE}/{ticker}/historical",
                          params={"assetclass": asset_class,
                                  "fromdate": start.isoformat(),
                                  "todate": end.isoformat(), "limit": 500},
                          headers=_UA, timeout=20)
            rows = ((r.json().get("data") or {}).get("tradesTable") or {}).get("rows") or []
        except (httpx.HTTPError, ValueError):
            continue
        out: dict[date, Decimal] = {}
        for row in rows:
            try:
                d = datetime.strptime(row["date"], "%m/%d/%Y").date()
                out[d] = Decimal(row["close"].replace("$", "").replace(",", ""))
            except (KeyError, ValueError):
                continue
        if out:
            return out
    return {}


def reference_close(ticker: str, on: date) -> Decimal | None:
    """The independent source's close for exactly that day, or None.

    Used by the one automated caller: deciding whether a fund order that our
    own providers have failed to price for over a day should be rejected, or
    is being held up by our side rather than by the fund. Returns None both
    when the reference has no such close and when it is out of range, because
    the caller treats "cannot confirm" the same either way.
    """
    if on < date.today() - timedelta(days=365 * MAX_LOOKBACK_YEARS):
        return None
    try:
        return _nasdaq_closes(ticker, on - timedelta(days=5), on).get(on)
    except Exception:                                            # noqa: BLE001
        log.exception("reference lookup failed for %s on %s", ticker, on)
        return None


def compare_close(ticker: str, on: date) -> Comparison:
    """Our stored close for `on` against Nasdaq's, for one symbol."""
    ticker = ticker.upper().strip()
    if on < date.today() - timedelta(days=365 * MAX_LOOKBACK_YEARS):
        return Comparison(ticker, on, None, None, None, "unavailable",
                          f"the reference source only carries about "
                          f"{MAX_LOOKBACK_YEARS} years of history")
    # close_exact, never close_on: the latter silently falls back to the nearest
    # earlier session, and comparing our Thursday against the reference's Friday
    # manufactures a disagreement out of two correct numbers. A cross-check that
    # reports false differences is worse than none — it teaches you to ignore it.
    try:
        ours = market_data.close_exact(ticker, on)
    except MarketDataError as exc:
        ours = None
        note = f"our price is unavailable: {exc}"
    else:
        note = ""

    theirs = _nasdaq_closes(ticker, on - timedelta(days=5), on).get(on)
    if theirs is None:
        return Comparison(ticker, on, ours, None, None, "unavailable",
                          note or "the reference source has no close for that day")
    if ours is None:
        return Comparison(
            ticker, on, None, theirs, None, "unavailable",
            note or f"we have no close printed for {on}, but the reference does "
                    f"({theirs}) — usually our provider has not published it yet")

    diff = (ours - theirs) / theirs * 100
    agree = abs(diff) <= TOLERANCE_PCT
    if not agree:
        # The most likely cause by far, and worth naming: the reference is raw,
        # so any split between `on` and today shows up as a clean whole-number
        # ratio rather than as a disagreement about price.
        ratio = theirs / ours if ours else Decimal(0)
        hint = (f" — the ratio is {ratio:.4f}; the reference does not restate "
                f"for splits, so a split since {on} explains a whole-number ratio"
                if ratio > Decimal("1.5") else "")
        note = f"prices disagree by {diff:.4f}%{hint}"
    else:
        note = f"agree to {abs(diff):.4f}%"
    return Comparison(ticker, on, ours, theirs, diff,
                      "agree" if agree else "differ", note)
