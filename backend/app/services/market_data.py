"""Market data layer with a pluggable provider chain.

Providers (all speak quote / history / dividends where supported):
  - PolygonProvider  (paid, POLYGON_API_KEY) — real-time trades where entitled,
    split-adjusted daily aggregates, reference dividends.
  - AlpacaProvider   (paid/free keys, ALPACA_API_KEY_ID/SECRET) — latest trade,
    split-adjusted daily bars. No dividend endpoint; dividends fall through.
  - YahooProvider    (free default, no key) — near-real-time quotes and
    split/dividend history from the public chart endpoint. Unofficial: data may
    be delayed and has no SLA.
  - NasdaqProvider   (free, no key) — daily closes for EQUITIES AND ETFs ONLY,
    as the backstop when Yahoo is unreachable. Split-adjusted and exact against
    the rest of the chain; its mutual-fund series is not, so funds are never
    asked of it.
  - SyntheticProvider — deterministic offline fallback: per-ticker geometric
    brownian paths (since 2015) and quarterly dividends, identical across
    processes with zero network.

MARKET_DATA_PROVIDER selects one provider explicitly, or `auto` builds the
chain [polygon?, alpaca?, yahoo] from configured keys. A failing provider
enters a short cool-down so the chain stays fast when a source is down.

SyntheticProvider is deliberately NOT part of `auto`: fabricated prices that
reach an order become permanent ledger rows. It is reachable only by asking
for it by name (MARKET_DATA_PROVIDER=synthetic), which the test suite does.
When every real provider is unreachable, callers get the last genuine cached
price (labelled "stale") or a MarketDataError — never an invented number. Historical closes are SPLIT-ADJUSTED so backtests through splits keep
correct share math. Quotes and candles are cached in Redis.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

import logging

import httpx
import redis

from app.config import get_settings
from app.rate_limit import get_redis

# Oldest date any history request will reach for. Yahoo carries adjusted
# closes well before this for long-lived funds; the floor exists so a typo'd
# backtest date cannot ask a provider for a century of candles. Raised history
# back to 2010 so imported brokerage records that predate 2015 can still be
# priced rather than valued at zero.
EPOCH = date(2010, 1, 1)
# The synthetic fallback's random walk is seeded from its own fixed origin, not
# from EPOCH: moving the history floor must not reshuffle every simulated price
# (which would silently restate every sandbox portfolio).
SYNTHETIC_ORIGIN = date(2015, 1, 1)
PRICE_Q = Decimal("0.0001")
_COOLDOWN = 300
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PaperTick/1.0"}


log = logging.getLogger("papertick.marketdata")


class MarketDataError(Exception):
    pass


class SymbolNotSupported(MarketDataError):
    """This provider does not cover this security — it is not malfunctioning.

    The distinction matters because a provider that errors is put in a
    cool-down and skipped for every symbol until it clears. Nasdaq is
    deliberately restricted to equities and ETFs, so it refuses every mutual
    fund by design; treating that as a fault took it out of the chain for the
    equities it serves perfectly well, which is precisely backwards.
    """


@dataclass
class Quote:
    ticker: str
    price: Decimal
    prev_close: Decimal | None
    as_of: datetime
    provider: str


@dataclass
class SymbolInfo:
    ticker: str
    name: str
    instrument_type: str  # EQUITY | ETF | MUTUALFUND
    currency: str
    exchange: str


def _dec(v) -> Decimal:
    return Decimal(str(v)).quantize(PRICE_Q, ROUND_HALF_UP)


# ------------------------------------------------------------- synthetic

def _u01(*parts: object) -> float:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return (int.from_bytes(h[:8], "big") + 1) / (2**64 + 2)


@lru_cache(maxsize=512)
def _synthetic_series(ticker: str, end_ordinal: int) -> tuple[tuple[int, float], ...]:
    end = date.fromordinal(end_ordinal)
    base = 15.0 + (int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4], "big") % 46500) / 100.0
    mu, sigma = 0.00035, 0.016
    out: list[tuple[int, float]] = []
    log_price = math.log(base)
    d = SYNTHETIC_ORIGIN
    i = 0
    while d <= end:
        if d.weekday() < 5:
            u1 = _u01(ticker, "r", i)
            u2 = _u01(ticker, "s", i)
            z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
            log_price += mu + sigma * z
            out.append((d.toordinal(), math.exp(log_price)))
            i += 1
        d += timedelta(days=1)
    return tuple(out)


class SyntheticProvider:
    name = "synthetic"
    has_dividends = True

    def history(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        series = _synthetic_series(ticker, min(end, date.today()).toordinal())
        s_ord = start.toordinal()
        return [(date.fromordinal(o), Decimal(str(round(p, 4)))) for o, p in series if o >= s_ord]

    def quote(self, ticker: str) -> Quote:
        now = datetime.now(timezone.utc)
        series = _synthetic_series(ticker, date.today().toordinal())
        if not series:
            raise MarketDataError(f"no synthetic data for {ticker}")
        last_close = Decimal(str(round(series[-1][1], 4)))
        prev_close = Decimal(str(round(series[-2][1], 4))) if len(series) > 1 else None
        wiggle = (_u01(ticker, "i", now.date(), now.hour) - 0.5) * 0.016
        price = (last_close * Decimal(str(1 + wiggle))).quantize(PRICE_Q, ROUND_HALF_UP)
        return Quote(ticker=ticker, price=price, prev_close=prev_close, as_of=now, provider=self.name)

    def dividends(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Deterministic quarterly dividends: annual yield 0.8%-2.8% by ticker hash,
        paid on the 15th of Mar/Jun/Sep/Dec (rolled to a weekday)."""
        annual_yield = 0.008 + _u01(ticker, "yield") * 0.02
        out: list[tuple[date, Decimal]] = []
        for year in range(start.year, end.year + 1):
            for month in (3, 6, 9, 12):
                d = date(year, month, 15)
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                if d < start or d > end or d > date.today():
                    continue
                series = _synthetic_series(ticker, d.toordinal())
                if not series:
                    continue
                px = series[-1][1]
                out.append((d, _dec(px * annual_yield / 4)))
        return out


# ------------------------------------------------------------- yahoo

class YahooProvider:
    name = "yahoo"
    has_dividends = True

    def _chart(self, ticker: str, params: dict) -> dict:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        with httpx.Client(timeout=5.0, headers=_UA) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        result = (data.get("chart") or {}).get("result")
        if not result:
            raise MarketDataError(f"no Yahoo data for {ticker}")
        return result[0]

    def quote(self, ticker: str) -> Quote:
        r = self._chart(ticker, {"range": "1d", "interval": "1d"})
        meta = r.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            raise MarketDataError(f"no Yahoo price for {ticker}")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        return Quote(
            ticker=ticker,
            price=_dec(price),
            prev_close=_dec(prev) if prev else None,
            as_of=datetime.now(timezone.utc),
            provider=self.name,
        )

    def _range_params(self, start: date, end: date) -> dict:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc).timestamp())
        return {"period1": p1, "period2": p2, "interval": "1d"}

    def history(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Split-adjusted closes — NOT dividend-adjusted.

        Yahoo returns two series and the difference is not cosmetic:

          quote.close  split-adjusted only. What the security actually traded
                       at that day, restated for later splits. AAPL 2015-01-02
                       comes back as 27.33, which is the real 109.33 divided by
                       the 2020 4:1 split.
          adjclose     split AND dividend adjusted — a total-return series in
                       which every past price is marked down by the
                       distributions paid since. Same day, same fund, VWELX:
                       39.51 raw against 17.04 adjusted, a 130% gap.

        This engine credits dividends separately, as cash, from the ex-date
        calendar. Buying at `adjclose` would therefore count every distribution
        twice — once by handing the backtest 2.3x too many shares, and again
        when the dividend is paid into settlement. Measured on a $10,000
        VWELX buy dated 2015-01-02, that reported $48,724 against a true
        $21,016. So: `quote.close`, and dividends stay a separate ledger entry.
        """
        r = self._chart(ticker, self._range_params(start, end))
        stamps = r.get("timestamp") or []
        indicators = r.get("indicators") or {}
        closes = (indicators.get("quote") or [{}])[0].get("close") or []
        if not closes:
            # Some thinly-traded funds only populate adjclose. It is the wrong
            # convention, but a priced position beats a zero-valued one, and
            # the two series converge as the window approaches the present.
            closes = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
        out: list[tuple[date, Decimal]] = []
        for ts, c in zip(stamps, closes):
            if c is None:
                continue
            out.append((datetime.fromtimestamp(ts, tz=timezone.utc).date(), _dec(c)))
        if not out:
            raise MarketDataError(f"no Yahoo history for {ticker}")
        return out

    def dividends(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        params = self._range_params(start, end)
        params["events"] = "div"
        r = self._chart(ticker, params)
        events = ((r.get("events") or {}).get("dividends") or {})
        out: list[tuple[date, Decimal]] = []
        for ev in events.values():
            amt = ev.get("amount")
            ts = ev.get("date")
            if amt is None or ts is None:
                continue
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            if start <= d <= end:
                out.append((d, _dec(amt)))
        return sorted(out)

    def splits(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Split ex-dates and their ratios (4.0 means 4-for-1, 0.5 a reverse).

        The chart endpoint has always returned these alongside dividends; they
        were simply never read. A split that is not applied to a holding does
        not look like an error — the share count stays put while the price is
        restated onto the new basis, so the position quietly loses the entire
        value of the split.
        """
        params = self._range_params(start, end)
        params["events"] = "div,split"
        r = self._chart(ticker, params)
        events = ((r.get("events") or {}).get("splits") or {})
        out: list[tuple[date, Decimal]] = []
        for ev in events.values():
            ts = ev.get("date")
            num, den = ev.get("numerator"), ev.get("denominator")
            if ts is None or not num or not den:
                continue
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            if start <= d <= end:
                out.append((d, (Decimal(str(num)) / Decimal(str(den)))))
        return sorted(out)

    # Yahoo short codes for the venues PaperTick can trade (US, USD).
    US_EXCHANGES = {
        "NMS", "NGM", "NCM", "NAS", "NSD",          # Nasdaq tiers / mutual funds
        "NYQ", "NYS", "PCX", "ASE", "AMX",          # NYSE, Arca, American
        "BTS", "CBO", "CBOE",                       # Cboe / BATS
        "PNK", "OQB", "OQX", "OTC",                 # OTC tiers
    }

    def search(self, q: str) -> list[dict]:
        """Symbol/company-name search via Yahoo's search endpoint, restricted to
        US-listed equities, ETFs and mutual funds (the tradable universe)."""
        with httpx.Client(timeout=5.0, headers=_UA) as client:
            resp = client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": 20, "newsCount": 0, "listsCount": 0},
            )
            resp.raise_for_status()
            data = resp.json()
        out = []
        for row in data.get("quotes") or []:
            sym = (row.get("symbol") or "").upper()
            qtype = row.get("quoteType") or ""
            exch = (row.get("exchange") or "").upper()
            if not sym or qtype not in {"EQUITY", "ETF", "MUTUALFUND"} or len(sym) > 12:
                continue
            # Yahoo suffixes foreign listings with a dot (APC.F, 0LO6.L);
            # US class shares use a hyphen (BRK-B), so a dot means non-US.
            if "." in sym or exch not in self.US_EXCHANGES:
                continue
            out.append({
                "ticker": sym,
                "name": row.get("longname") or row.get("shortname") or sym,
                "type": qtype,
                "exchange": row.get("exchDisp") or exch,
            })
        return out

    def lookup(self, ticker: str) -> SymbolInfo | None:
        try:
            r = self._chart(ticker, {"range": "5d", "interval": "1d"})
        except (httpx.HTTPError, MarketDataError, ValueError):
            return None
        meta = r.get("meta") or {}
        itype = meta.get("instrumentType") or ""
        if itype not in {"EQUITY", "ETF", "MUTUALFUND"}:
            return None
        return SymbolInfo(
            ticker=(meta.get("symbol") or ticker).upper(),
            name=meta.get("longName") or meta.get("shortName") or ticker.upper(),
            instrument_type=itype,
            currency=meta.get("currency") or "",
            exchange=meta.get("exchangeName") or meta.get("fullExchangeName") or "",
        )


# ------------------------------------------------------------- polygon (paid, optional)

class PolygonProvider:
    name = "polygon"
    has_dividends = True

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        with httpx.Client(timeout=5.0, base_url="https://api.polygon.io") as client:
            resp = client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    def quote(self, ticker: str) -> Quote:
        prev = None
        try:
            pdata = self._get(f"/v2/aggs/ticker/{ticker}/prev", {"adjusted": "true"})
            results = pdata.get("results") or []
            if results:
                prev = _dec(results[0]["c"])
        except httpx.HTTPError:
            pass
        price = None
        try:  # real-time last trade needs the paid entitlement; fall back to prev close
            t = self._get(f"/v2/last/trade/{ticker}")
            price = _dec((t.get("results") or {}).get("p"))
        except (httpx.HTTPStatusError, httpx.HTTPError, TypeError, KeyError):
            price = prev
        if price is None:
            raise MarketDataError(f"no Polygon price for {ticker}")
        return Quote(ticker=ticker, price=price, prev_close=prev,
                     as_of=datetime.now(timezone.utc), provider=self.name)

    def history(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        data = self._get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            # Polygon's `adjusted` is splits only — it never dividend-adjusts —
            # so this already matches the convention the others are pinned to.
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        results = data.get("results") or []
        out = [
            (datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date(), _dec(r["c"]))
            for r in results
            if r.get("c") is not None
        ]
        if not out:
            raise MarketDataError(f"no Polygon history for {ticker}")
        return out

    def dividends(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        data = self._get("/v3/reference/dividends", {
            "ticker": ticker,
            "ex_dividend_date.gte": start.isoformat(),
            "ex_dividend_date.lte": end.isoformat(),
            "limit": 1000,
        })
        out = []
        for r in data.get("results") or []:
            exd = r.get("ex_dividend_date")
            amt = r.get("cash_amount")
            if exd and amt:
                out.append((date.fromisoformat(exd), _dec(amt)))
        return sorted(out)


# ------------------------------------------------------------- alpaca (optional)

class NasdaqProvider:
    """Nasdaq's public endpoint — free, keyless, EQUITIES AND ETFs ONLY.

    Included as the backstop for a Yahoo outage, because it is the only free
    source left that needs no key and still answers. Measured against the
    verified chain it is exact, not merely close — same day, same price, to the
    fourth decimal across eight separate corporate actions:

        AAPL 4:1 2020   GOOG 20:1 2022   NVDA 10:1 2024   TSLA 3:1 2022
        SCHD 3:1 2024   TQQQ 2:1 2022    SOXL 15:1 2021   LABU 1:20 rev 2023

    Two hard limits shape this class, both established by measurement:

    1. MUTUAL FUNDS ARE RAW. `assetclass=mutualfunds` returns prices with no
       split restatement at all — FCNTX on 2018-08-08 comes back as $138.17
       against a true $13.82, a clean factor of ten. So that asset class is
       never requested. The omission is the safety mechanism: a fund symbol
       simply finds no data here and the request fails, instead of quietly
       pricing a holding an order of magnitude wrong.

    2. `todate` IN THE PAST IS UNRELIABLE. A historical `todate` returns HTTP
       200 with zero rows for most windows (deterministically — six identical
       repeats), while ignoring `fromdate` in the one window that does answer.
       Only `todate=today` behaves, so every request asks for the full span to
       the present and the window is applied locally.
    """

    name = "nasdaq"
    has_dividends = False
    # deliberately excludes "mutualfunds"; see the class docstring
    ASSET_CLASSES = ("stocks", "etf")
    # how far back the endpoint carries data before it silently truncates
    MAX_LOOKBACK_DAYS = 3650

    def _fetch(self, ticker: str, start: date) -> dict[date, Decimal]:
        floor = date.today() - timedelta(days=self.MAX_LOOKBACK_DAYS)
        params = {
            "fromdate": max(start, floor).isoformat(),
            # never a historical todate — see the class docstring
            "todate": date.today().isoformat(),
            "limit": 5000,
        }
        headers = {**_UA, "Accept": "application/json"}
        for asset_class in self.ASSET_CLASSES:
            with httpx.Client(timeout=15.0, headers=headers) as client:
                resp = client.get(
                    f"https://api.nasdaq.com/api/quote/{ticker}/historical",
                    params={**params, "assetclass": asset_class},
                )
                resp.raise_for_status()
                payload = resp.json()
            rows = (((payload.get("data") or {}).get("tradesTable") or {})
                    .get("rows") or [])
            out: dict[date, Decimal] = {}
            for row in rows:
                try:
                    d = datetime.strptime(row["date"], "%m/%d/%Y").date()
                    out[d] = _dec(row["close"].replace("$", "").replace(",", ""))
                except (KeyError, ValueError, TypeError):
                    continue
            if out:
                return out
        return {}

    def history(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        rows = self._fetch(ticker, start)
        if not rows:
            raise SymbolNotSupported(
                f"Nasdaq carries equities and ETFs only; {ticker} is neither")
        out = sorted((d, p) for d, p in rows.items() if start <= d <= end)
        if not out:
            raise MarketDataError(f"no Nasdaq history for {ticker} in that window")
        return out

    def quote(self, ticker: str) -> Quote:
        rows = self._fetch(ticker, date.today() - timedelta(days=10))
        if not rows:
            raise SymbolNotSupported(
                f"Nasdaq carries equities and ETFs only; {ticker} is neither")
        days = sorted(rows)
        prev = rows[days[-2]] if len(days) > 1 else None
        # a daily close, not a live print: this is a fallback for when the
        # real-time sources are unreachable, and it says so
        return Quote(ticker=ticker, price=rows[days[-1]], prev_close=prev,
                     as_of=datetime.combine(days[-1], time(20, 0), tzinfo=timezone.utc),
                     provider=self.name)


class TiingoProvider:
    """Tiingo — paid tiers plus a free personal tier, covers mutual funds.

    The reason it is here rather than another equities API: Tiingo carries
    mutual-fund NAVs, which Polygon and Alpaca do not. A portfolio of Vanguard
    and Fidelity funds cannot be priced by an equities-only vendor at all, so
    for this application it is the only viable second source.

    Adjustment is explicit and per-field: `close` is the price as printed,
    `adjClose` is split *and* dividend adjusted, and `splitFactor` carries the
    corporate action. PaperTick needs split-only, which neither field gives
    directly, so it is reconstructed by walking the cumulative split factor
    back from the present — the same restatement Yahoo applies to
    `quote.close`. The convention probe verifies the result rather than
    trusting this comment.
    """

    name = "tiingo"
    has_dividends = True

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Token {token}",
                        "Content-Type": "application/json"}

    def _get(self, path: str, params: dict | None = None):
        with httpx.Client(timeout=8.0, base_url="https://api.tiingo.com",
                          headers=self.headers) as client:
            resp = client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    def _daily(self, ticker: str, start: date, end: date) -> list[dict]:
        return self._get(f"/tiingo/daily/{ticker}/prices", {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "format": "json",
        }) or []

    def history(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Split-adjusted closes, rebuilt from the raw close and splitFactor.

        Tiingo returns `close` unrestated, so a series spanning a split has a
        step in it. Walking backwards from the newest bar and dividing by each
        split factor as it is passed restates the earlier prices onto today's
        share basis, which is exactly what split adjustment means.
        """
        rows = self._daily(ticker, start, end)
        if not rows:
            raise MarketDataError(f"no Tiingo history for {ticker}")
        parsed: list[tuple[date, Decimal, Decimal]] = []
        for r in rows:
            close = r.get("close")
            if close is None:
                continue
            parsed.append((
                datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date(),
                _dec(close),
                Decimal(str(r.get("splitFactor") or 1)),
            ))
        parsed.sort(key=lambda x: x[0])

        out: list[tuple[date, Decimal]] = []
        cumulative = Decimal(1)
        # newest -> oldest: a split on day X applies to every bar before X
        for d, close, factor in reversed(parsed):
            out.append((d, (close / cumulative).quantize(PRICE_Q, ROUND_HALF_UP)))
            if factor and factor != 1:
                cumulative *= factor
        out.reverse()
        return out

    def quote(self, ticker: str) -> Quote:
        data = self._get(f"/iex/{ticker}") or []
        row = data[0] if data else {}
        price = row.get("last") or row.get("tngoLast") or row.get("prevClose")
        if price is None:
            # IEX feed is equities/ETFs only; funds price off the daily NAV
            today = date.today()
            rows = self._daily(ticker, today - timedelta(days=10), today)
            if not rows:
                raise MarketDataError(f"no Tiingo quote for {ticker}")
            price, prev = rows[-1].get("close"), (rows[-2].get("close") if len(rows) > 1 else None)
            if price is None:
                raise MarketDataError(f"no Tiingo quote for {ticker}")
            return Quote(ticker=ticker, price=_dec(price),
                         prev_close=_dec(prev) if prev is not None else None,
                         as_of=datetime.now(timezone.utc), provider=self.name)
        prev = row.get("prevClose")
        return Quote(ticker=ticker, price=_dec(price),
                     prev_close=_dec(prev) if prev is not None else None,
                     as_of=datetime.now(timezone.utc), provider=self.name)

    def splits(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        out: list[tuple[date, Decimal]] = []
        for r in self._daily(ticker, start, end):
            factor = r.get("splitFactor")
            if factor and Decimal(str(factor)) != 1:
                d = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date()
                out.append((d, Decimal(str(factor))))
        return sorted(out)

    def dividends(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        out: list[tuple[date, Decimal]] = []
        for r in self._daily(ticker, start, end):
            amount = r.get("divCash")
            if amount:
                d = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date()
                out.append((d, _dec(amount)))
        return out


class AlpacaProvider:
    name = "alpaca"
    has_dividends = False

    def __init__(self, key_id: str, secret: str):
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}

    def _get(self, path: str, params: dict | None = None) -> dict:
        with httpx.Client(timeout=5.0, base_url="https://data.alpaca.markets",
                          headers=self.headers) as client:
            resp = client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    def quote(self, ticker: str) -> Quote:
        data = self._get(f"/v2/stocks/{ticker}/trades/latest")
        trade = data.get("trade") or {}
        if trade.get("p") is None:
            raise MarketDataError(f"no Alpaca price for {ticker}")
        prev = None
        try:
            bars = self.history(ticker, date.today() - timedelta(days=7), date.today())
            if bars:
                prev = bars[-1][1]
        except MarketDataError:
            pass
        return Quote(ticker=ticker, price=_dec(trade["p"]), prev_close=prev,
                     as_of=datetime.now(timezone.utc), provider=self.name)

    def history(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        data = self._get(f"/v2/stocks/{ticker}/bars", {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            # "split", not "all": Alpaca's "all" is split AND dividend adjusted,
            # a total-return series. Dividends are paid separately here, so that
            # would count every distribution twice. Every provider in this file
            # must agree on the convention or a ledger built from two of them is
            # silently wrong.
            "adjustment": "split",
            "limit": 10000,
        })
        bars = data.get("bars") or []
        out = [
            (datetime.fromisoformat(b["t"].replace("Z", "+00:00")).date(), _dec(b["c"]))
            for b in bars
            if b.get("c") is not None
        ]
        if not out:
            raise MarketDataError(f"no Alpaca history for {ticker}")
        return out


# ------------------------------------------------------------- composite service

_PROVIDER_ERRORS = (httpx.HTTPError, MarketDataError, ValueError, KeyError, TypeError)


class MarketDataService:
    def __init__(self) -> None:
        self.synthetic = SyntheticProvider()
        self.yahoo = YahooProvider()
        self.nasdaq = NasdaqProvider()

    def _chain(self) -> list:
        s = get_settings()
        polygon = PolygonProvider(s.polygon_api_key) if s.polygon_api_key else None
        alpaca = (
            AlpacaProvider(s.alpaca_api_key_id, s.alpaca_api_secret)
            if s.alpaca_api_key_id and s.alpaca_api_secret
            else None
        )
        tiingo = TiingoProvider(s.tiingo_api_token) if s.tiingo_api_token else None
        mode = s.market_data_provider
        if mode == "synthetic":
            return [self.synthetic]
        if mode == "yahoo":
            return [self.yahoo]
        if mode == "nasdaq":
            return [self.nasdaq]
        if mode == "tiingo":
            if tiingo is None:
                raise MarketDataError("TIINGO_API_TOKEN is not configured")
            return [tiingo]
        if mode == "polygon":
            if polygon is None:
                raise MarketDataError("POLYGON_API_KEY is not configured")
            return [polygon]
        if mode == "alpaca":
            if alpaca is None:
                raise MarketDataError("ALPACA_API_KEY_ID / ALPACA_API_SECRET are not configured")
            return [alpaca]
        # Synthetic is NOT in the auto chain. It invents prices, and a fill is
        # permanent: an order that executed against a fabricated quote during a
        # provider outage would sit in the ledger forever, indistinguishable
        # from a real one, silently corrupting cost basis, returns and tax
        # lots. When the real sources are unreachable the honest answer is to
        # fail — callers hold the order and retry, which is the same thing the
        # outage catch-up does. Synthetic is reachable only by asking for it
        # explicitly with MARKET_DATA_PROVIDER=synthetic.
        # Order is deliberate: paid, entitled sources first; Yahoo last because
        # it is unofficial. Tiingo sits above Alpaca because it carries mutual
        # funds, which Alpaca and Polygon do not.
        # Nasdaq goes last, behind Yahoo: it is free and exact on equities but
        # serves daily closes rather than live prints, and it cannot price a
        # mutual fund at all. That makes it a backstop for a Yahoo outage
        # rather than a peer — a fund request simply falls past it and fails,
        # which is the intended behaviour.
        nasdaq = self.nasdaq if s.nasdaq_fallback else None
        chain = [p for p in (polygon, tiingo, alpaca, self.yahoo, nasdaq)
                 if p is not None]
        # A provider measured to be on the wrong price convention is worse than
        # a missing one: its numbers become permanent ledger rows.
        from app.services.convention import quarantined

        live = [p for p in chain if not quarantined(p.name)]
        if not live and chain:
            # Everything is quarantined. Failing closed here is correct — the
            # callers hold orders and serve stale cached prices rather than
            # writing prices we have proven are on the wrong basis.
            log.error("every market data provider is quarantined on price convention")
        return live

    # ---- cool-down bookkeeping

    def _available(self, provider) -> bool:
        try:
            return get_redis().get(f"md:down:{provider.name}") is None
        except redis.RedisError:
            return True

    def _mark_down(self, provider) -> None:
        try:
            get_redis().set(f"md:down:{provider.name}", "1", ex=_COOLDOWN)
        except redis.RedisError:
            pass

    def _try_chain(self, op):
        chain = self._chain()
        last_err: Exception | None = None
        for provider in chain:
            if len(chain) > 1 and provider is not self.synthetic and not self._available(provider):
                continue
            try:
                return op(provider)
            except SymbolNotSupported as exc:
                # Out of scope, not broken. Move on without a cool-down.
                last_err = exc
            except _PROVIDER_ERRORS as exc:
                last_err = exc
                if provider is not self.synthetic:
                    self._mark_down(provider)
        raise MarketDataError(str(last_err) if last_err else "no market data provider available")

    # ---- cache helpers

    def _cache_get(self, key: str):
        try:
            raw = get_redis().get(key)
            return json.loads(raw) if raw else None
        except (redis.RedisError, json.JSONDecodeError):
            return None

    def _cache_set(self, key: str, value, ttl: int) -> None:
        try:
            get_redis().set(key, json.dumps(value), ex=ttl)
        except redis.RedisError:
            pass

    # ---- upstream budget

    def _budget_ok(self) -> bool:
        """Claim one outbound provider call from the shared per-minute budget.

        Every process that talks to a provider — api, worker, beat — counts
        against one bucket, because the thing being protected is the upstream
        endpoint's view of this deployment, not any single container. The
        window is fixed rather than sliding: a minute-granular counter is
        enough to keep a runaway loop off an unofficial endpoint, and it costs
        one INCR.

        Fails *open* on a Redis error. The budget is politeness towards the
        provider, not a security control, and a Redis blip must not take
        pricing down with it.
        """
        limit = get_settings().market_upstream_per_minute
        if limit <= 0:
            return True
        try:
            r = get_redis()
            key = f"md:budget:{int(datetime.now(timezone.utc).timestamp() // 60)}"
            used = r.incr(key)
            if used == 1:
                r.expire(key, 120)
            return used <= limit
        except redis.RedisError:
            return True

    # ---- public API

    def quote(self, ticker: str) -> Quote:
        key = f"md:q:{ticker}"
        cached = self._cache_get(key)
        if cached:
            return Quote(
                ticker=ticker,
                price=Decimal(cached["price"]),
                prev_close=Decimal(cached["prev_close"]) if cached.get("prev_close") else None,
                as_of=datetime.fromisoformat(cached["as_of"]),
                provider=cached["provider"],
            )
        def stale_or(exc: MarketDataError) -> Quote:
            """Last real price, or the error. Never an invented one.

            A stale genuine quote is honest — it is labelled, and it is what
            the market last actually printed. Reaching for the synthetic
            generator here instead would hand a caller a number no exchange
            ever produced, and callers write those to the ledger.
            """
            stale = self._cache_get(f"{key}:stale")
            if not stale:
                raise exc
            return Quote(
                ticker=ticker,
                price=Decimal(stale["price"]),
                prev_close=Decimal(stale["prev_close"]) if stale.get("prev_close") else None,
                as_of=datetime.fromisoformat(stale["as_of"]),
                provider=stale["provider"] + " (stale)",
            )

        if not self._budget_ok():
            return stale_or(MarketDataError(
                "market data request budget exhausted; try again shortly"))
        try:
            q = self._try_chain(lambda p: p.quote(ticker))
        except MarketDataError as exc:
            return stale_or(exc)
        payload = {
            "price": str(q.price),
            "prev_close": str(q.prev_close) if q.prev_close is not None else None,
            "as_of": q.as_of.isoformat(),
            "provider": q.provider,
        }
        ttl = get_settings().quote_cache_seconds
        self._cache_set(key, payload, ttl=ttl)
        # Grace copy for the budget-exhausted path above, and for a provider
        # outage: kept far longer than the hot entry, never read while the hot
        # entry is alive.
        self._cache_set(f"{key}:stale", payload, ttl=max(ttl * 20, 3600))
        return q

    def history(self, ticker: str, start: date, end: date,
                force_refresh: bool = False) -> tuple[list[tuple[date, Decimal]], str]:
        start = max(start, EPOCH)
        key = f"md:h:{ticker}:{start.isoformat()}:{end.isoformat()}"
        if not force_refresh:
            cached = self._cache_get(key)
            if cached:
                return (
                    [(date.fromisoformat(d), Decimal(p)) for d, p in cached["candles"]],
                    cached["provider"],
                )
        provider_name = {}

        def op(p):
            provider_name["name"] = p.name
            return p.history(ticker, start, end)

        if not self._budget_ok():
            cached = self._cache_get(key)
            if cached:
                return (
                    [(date.fromisoformat(d), Decimal(p)) for d, p in cached["candles"]],
                    cached["provider"],
                )
            raise MarketDataError(
                "market data request budget exhausted; try again shortly"
            )
        try:
            candles = self._try_chain(op)
        except MarketDataError:
            # A stale candle set beats a fabricated one for the same reason as
            # quotes; with no cache at all the caller must hear about it.
            cached = self._cache_get(key)
            if cached:
                return (
                    [(date.fromisoformat(d), Decimal(p)) for d, p in cached["candles"]],
                    cached["provider"],
                )
            raise
        self._cache_set(key, {
            "provider": provider_name["name"],
            "candles": [(d.isoformat(), str(px)) for d, px in candles],
        }, ttl=get_settings().history_cache_seconds)
        return candles, provider_name["name"]

    def dividends(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        key = f"md:d:{ticker}:{start.isoformat()}:{end.isoformat()}"
        cached = self._cache_get(key)
        if cached is not None:
            return [(date.fromisoformat(d), Decimal(a)) for d, a in cached]
        chain = [p for p in self._chain() if getattr(p, "has_dividends", False)]
        if not chain:  # e.g. explicit alpaca mode, which has no dividend endpoint
            chain = [self.synthetic] if get_settings().market_data_provider == "synthetic" \
                else [self.yahoo]
        events: list[tuple[date, Decimal]] = []
        if not self._budget_ok():
            # No stale fallback here on purpose: an empty list would be read as
            # "this security pays nothing" and could claw back a real credit.
            raise MarketDataError(
                "market data request budget exhausted; try again shortly"
            )
        for provider in chain:
            if provider is not self.synthetic and not self._available(provider):
                continue
            try:
                events = provider.dividends(ticker, start, end)
                break
            except _PROVIDER_ERRORS:
                if provider is not self.synthetic:
                    self._mark_down(provider)
        self._cache_set(key, [(d.isoformat(), str(a)) for d, a in events],
                        ttl=get_settings().dividend_cache_seconds)
        return events

    def splits(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Split events, cached. Empty when no provider in the chain reports them."""
        key = f"md:sp:{ticker}:{start.isoformat()}:{end.isoformat()}"
        cached = self._cache_get(key)
        if cached is not None:
            return [(date.fromisoformat(d), Decimal(r)) for d, r in cached]
        events: list[tuple[date, Decimal]] = []
        for provider in self._chain():
            if not hasattr(provider, "splits"):
                continue
            try:
                events = provider.splits(ticker, start, end)
                break
            except _PROVIDER_ERRORS:
                if provider is not self.synthetic:
                    self._mark_down(provider)
        self._cache_set(key, [(d.isoformat(), str(r)) for d, r in events],
                        ttl=get_settings().dividend_cache_seconds)
        return events

    def close_on(self, ticker: str, d: date) -> Decimal | None:
        """Close on d, falling back to the nearest prior trading day."""
        candles, _ = self.history(ticker, d - timedelta(days=10), d)
        best: Decimal | None = None
        for cd, price in candles:
            if cd <= d:
                best = price
        return best

    def close_exact(self, ticker: str, d: date) -> Decimal | None:
        """The close/NAV printed exactly for day d, or None if not yet published.

        For a recent day a cache miss forces a refetch, so a just-published
        close is picked up promptly. That refetch is throttled per
        (ticker, day): a fund order waiting on its NAV is re-examined by the
        worker every 60s, but the exchange itself is not — a NAV publishes once
        and then stops changing, so asking more often than
        NAV_POLL_INTERVAL_SECONDS buys nothing and spends the upstream budget
        for up to 30 hours per unfilled order.
        """
        candles, _ = self.history(ticker, d - timedelta(days=5), d)
        for cd, price in candles:
            if cd == d:
                return price
        if (date.today() - d).days > 2:
            return None

        gate = f"md:navpoll:{ticker}:{d.isoformat()}"
        interval = max(1, get_settings().nav_poll_interval_seconds)
        try:
            # SET NX EX is the whole throttle: the first caller in the window
            # claims the refetch, everyone else reads the cache and waits.
            fresh = bool(get_redis().set(gate, "1", nx=True, ex=interval))
        except redis.RedisError:
            fresh = True
        if not fresh:
            return None

        candles, _ = self.history(ticker, d - timedelta(days=5), d, force_refresh=True)
        for cd, price in candles:
            if cd == d:
                return price
        return None

    def lookup_symbol(self, ticker: str) -> SymbolInfo | None:
        """Validate an unknown symbol against Yahoo metadata (used for
        auto-registration). Returns None when offline or not a US-dollar
        equity/ETF/mutual fund."""
        if get_settings().market_data_provider == "synthetic":
            return None
        return self.yahoo.lookup(ticker)

    def search_symbols(self, q: str) -> list[dict]:
        """Company-name / ticker search (cached). Empty when offline."""
        if get_settings().market_data_provider == "synthetic":
            return []
        key = f"md:s:{q.lower()}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            results = self.yahoo.search(q)
        except _PROVIDER_ERRORS:
            results = []
        self._cache_set(key, results, ttl=300)
        return results


market_data = MarketDataService()
