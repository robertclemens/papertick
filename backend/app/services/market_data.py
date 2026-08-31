"""Market data layer with a pluggable provider chain.

Providers (all speak quote / history / dividends where supported):
  - PolygonProvider  (paid, POLYGON_API_KEY) — real-time trades where entitled,
    split-adjusted daily aggregates, reference dividends.
  - AlpacaProvider   (paid/free keys, ALPACA_API_KEY_ID/SECRET) — latest trade,
    adjusted daily bars. No dividend endpoint; dividends fall through the chain.
  - YahooProvider    (free default, no key) — near-real-time quotes and
    split/dividend history from the public chart endpoint. Unofficial: data may
    be delayed and has no SLA.
  - SyntheticProvider — deterministic offline fallback: per-ticker geometric
    brownian paths (since 2015) and quarterly dividends, identical across
    processes with zero network.

MARKET_DATA_PROVIDER selects one provider explicitly, or `auto` builds the
chain [polygon?, alpaca?, yahoo, synthetic] from configured keys. A failing
provider enters a short cool-down so the chain stays fast when a source is
down. Historical closes are SPLIT-ADJUSTED so backtests through splits keep
correct share math. Quotes and candles are cached in Redis.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

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


class MarketDataError(Exception):
    pass


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
        r = self._chart(ticker, self._range_params(start, end))
        stamps = r.get("timestamp") or []
        indicators = r.get("indicators") or {}
        # split-adjusted closes keep backtest share math correct across splits
        adj = (indicators.get("adjclose") or [{}])[0].get("adjclose")
        closes = adj or ((indicators.get("quote") or [{}])[0].get("close") or [])
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
            "adjustment": "all",
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

    def _chain(self) -> list:
        s = get_settings()
        polygon = PolygonProvider(s.polygon_api_key) if s.polygon_api_key else None
        alpaca = (
            AlpacaProvider(s.alpaca_api_key_id, s.alpaca_api_secret)
            if s.alpaca_api_key_id and s.alpaca_api_secret
            else None
        )
        mode = s.market_data_provider
        if mode == "synthetic":
            return [self.synthetic]
        if mode == "yahoo":
            return [self.yahoo]
        if mode == "polygon":
            if polygon is None:
                raise MarketDataError("POLYGON_API_KEY is not configured")
            return [polygon]
        if mode == "alpaca":
            if alpaca is None:
                raise MarketDataError("ALPACA_API_KEY_ID / ALPACA_API_SECRET are not configured")
            return [alpaca]
        chain = [p for p in (polygon, alpaca, self.yahoo) if p is not None]
        chain.append(self.synthetic)
        return chain

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
        q = self._try_chain(lambda p: p.quote(ticker))
        self._cache_set(key, {
            "price": str(q.price),
            "prev_close": str(q.prev_close) if q.prev_close is not None else None,
            "as_of": q.as_of.isoformat(),
            "provider": q.provider,
        }, ttl=30)
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

        candles = self._try_chain(op)
        self._cache_set(key, {
            "provider": provider_name["name"],
            "candles": [(d.isoformat(), str(px)) for d, px in candles],
        }, ttl=3600)
        return candles, provider_name["name"]

    def dividends(self, ticker: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        key = f"md:d:{ticker}:{start.isoformat()}:{end.isoformat()}"
        cached = self._cache_get(key)
        if cached is not None:
            return [(date.fromisoformat(d), Decimal(a)) for d, a in cached]
        chain = [p for p in self._chain() if getattr(p, "has_dividends", False)]
        if not chain:  # e.g. explicit alpaca mode: fall back to yahoo, then synthetic
            chain = [self.yahoo, self.synthetic]
        events: list[tuple[date, Decimal]] = []
        for provider in chain:
            if provider is not self.synthetic and not self._available(provider):
                continue
            try:
                events = provider.dividends(ticker, start, end)
                break
            except _PROVIDER_ERRORS:
                if provider is not self.synthetic:
                    self._mark_down(provider)
        self._cache_set(key, [(d.isoformat(), str(a)) for d, a in events], ttl=6 * 3600)
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
        close is picked up promptly."""
        candles, _ = self.history(ticker, d - timedelta(days=5), d)
        for cd, price in candles:
            if cd == d:
                return price
        if (date.today() - d).days <= 2:
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
