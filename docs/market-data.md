# Market data

Where every number in PaperTick comes from, and the rules that keep a
multi-provider chain from quietly corrupting a ledger. Configuration for all
of this lives in [`.env.example`](../.env.example).

Nothing outside this page is fetched at runtime.

## Price and dividend providers

| Provider | Endpoint | Auth | Serves | Notes |
|---|---|---|---|---|
| **Polygon.io** | `api.polygon.io` | `POLYGON_API_KEY` | Trades, split-adjusted daily aggregates, dividends | Real-time where your plan is entitled. **No mutual funds** |
| **Tiingo** | `api.tiingo.com` | `TIINGO_API_TOKEN` | Daily prices, splits, dividends | **The only paid vendor here that carries mutual-fund NAVs.** Free personal tier plus paid plans |
| **Alpaca** | `data.alpaca.markets` | `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET` | Latest trade, split-adjusted daily bars | No dividend endpoint — dividends fall through. **No mutual funds** |
| **Yahoo Finance** | `query1.finance.yahoo.com` | none | Quotes, split/dividend history, symbol search | **Unofficial.** Free default. May be delayed, has no SLA, and can change or block without notice |
| **Nasdaq** | `api.nasdaq.com` | none | Daily closes, **equities and ETFs only** | Free keyless backstop for a Yahoo outage. Last in the chain — see below |
| **Synthetic** | — | none | Deterministic price paths and quarterly dividends since 2015 | **Opt-in only.** Never part of `auto` — see below |

`MARKET_DATA_PROVIDER` selects one explicitly, or `auto` (the default) builds the chain
**Polygon → Tiingo → Alpaca → Yahoo → Nasdaq** from whichever keys are configured. Tiingo sits
above Alpaca because it prices mutual funds, which the equities-only vendors cannot —
a portfolio of Vanguard or Fidelity funds is unpriceable without Yahoo or Tiingo. A provider that errors
enters a 5-minute cool-down so the chain stays fast while a source is down, and every
quote is labeled with the provider that actually served it.

> **Synthetic prices are never substituted for real ones.** A fill is permanent: an
> order executed against a fabricated quote during a provider outage would sit in the
> ledger forever, indistinguishable from a real trade, silently corrupting cost basis,
> returns and tax lots. So `auto` contains real providers only. When all of them are
> unreachable, a reader gets the last genuine cached price (labeled `stale`) or a clean
> error, and a **due order is held and retried rather than filled or rejected** —
> bounded by `MARKET_DATA_GIVE_UP_HOURS` (default 24). Set
> `MARKET_DATA_PROVIDER=synthetic` explicitly for an offline sandbox.

History reaches back to 2010; the synthetic walk starts 2015. No free source guarantees
exact real-time data — for that, plug in a paid key.

## One price convention, enforced across every provider

Historical closes are **split-adjusted and deliberately NOT dividend-adjusted**. This is
the single most important correctness rule in the data layer, and it is what makes a
multi-provider chain safe at all.

Vendors ship two different series and both are called "the close":

| Series | Meaning | VWELX, 2015-01-02 |
|---|---|---|
| split-adjusted | what it actually traded at, restated for later splits | **$39.16** |
| total return (split **+ dividend** adjusted) | every past price marked down by distributions paid since | $17.04 |

A 130% gap on the same fund, same day, same vendor. This engine pays dividends
separately as cash from the ex-date calendar, so pricing off the total-return series
counts every distribution **twice** — once as extra shares, once as the credit. On a
$10,000 VWELX buy dated 2015-01-02 that reported **$48,724 against a true $21,207**.

So each provider is pinned to splits-only: Yahoo reads `quote.close` (never `adjclose`),
Alpaca uses `adjustment=split` (never `all`), Polygon's `adjusted` flag is splits-only by
definition, and Tiingo's raw closes are restated using its own `splitFactor`.

## The convention is verified, not just pinned

A query parameter is a promise the vendor makes, not a fact anyone checked — and vendors
change defaults and quietly restate series. A ledger on the wrong convention is not
visibly broken: every number still looks like a price. So each provider is measured
against fixtures whose answers are permanent historical fact:

| Fixture | Window | Separates |
|---|---|---|
| AAPL | 2015-01-02 → 2021-01-04 | spans the 2020 4:1 split — tells `raw` (1.1837) from `split` (4.7347) from `total_return` (5.1975) |
| VWELX | 2015-01-02 → 2021-01-04 | no splits, heavy distributions — tells `split` (1.1228) from `total_return` (1.6517) |

Both fixtures are needed and neither is sufficient alone: a symbol that never split
cannot tell `raw` from `split`, and a symbol with no distributions cannot tell `split`
from `total_return`. The test uses the **ratio** between two dates that are both already
in the past, so a future split cannot invalidate the fixture — a split-adjusted series
restates both endpoints equally and its ratio never moves.

**Enforcement runs on every fill; measurement does not.** Checking a stored verdict costs
0.13 ms (one Redis read) and already happens on every market-data call, so there is no
reason to make it less frequent. Re-deriving the verdict costs ~800 ms and two requests,
so it is gated on freshness at the moment a price becomes a permanent ledger row: a
verdict older than `CONVENTION_MAX_AGE_HOURS` (default 24) is re-measured first, once,
shared by every process. A deployment that is not trading never probes at all; one that
is trading is never more than a day behind a vendor changing convention. Set it to `0`
to re-measure before every fill.

A provider proven to be on another convention is **quarantined** — dropped from the chain
rather than allowed to write prices onto the wrong basis, since orders are held safely
but bad fills are permanent. A provider that merely fails to answer is marked `unknown`
and keeps working: being down is not the same as lying. Inspect verdicts at
`GET /api/v1/market/providers`; disable with `CONVENTION_QUARANTINE=false`.

## Nasdaq: the keyless fallback, equities and ETFs only

Yahoo is unofficial and could stop answering at any time, so the chain ends in a free
source that needs no key. It earns that slot on measurement, not reputation: against the
verified chain it is **exact to the fourth decimal across eight corporate actions** —
AAPL 4:1, GOOG 20:1, NVDA 10:1, TSLA 3:1, SCHD 3:1, TQQQ 2:1, SOXL 15:1 and a LABU 1:20
reverse split.

Two measured limits shape how it is used, and both are enforced in code:

- **Mutual funds are raw.** Its `mutualfunds` series is not split-adjusted — FCNTX on
  2018-08-08 returns $138.17 against a true $13.82, a clean factor of ten. So that asset
  class is **never requested**. The omission *is* the safety mechanism: a fund finds no
  data here and falls past it, rather than being priced an order of magnitude wrong.
- **A historical `todate` is unreliable.** It returns HTTP 200 with zero rows for most
  past windows (deterministically), while ignoring `fromdate` in the one that answers.
  Every request therefore asks to the present and the window is applied locally.

It sits **last, behind Yahoo**, because it serves daily closes rather than live prints —
a backstop, not a peer. It is verified by the same convention probe as everything else
(which is why the fixture set includes equity windows: an equity-only provider can reach
no fund fixture, and an unverifiable provider that is still used is exactly the gap the
probe exists to close). Disable with `NASDAQ_FALLBACK=false`.

A provider declining a symbol it never covered is distinguished from one that is
malfunctioning: `SymbolNotSupported` moves to the next provider **without** a cool-down,
so pricing a fund cannot take Nasdaq out of the chain for the equities it serves.

## Verifying a price against an independent source

`GET /api/v1/market/verify/{ticker}?on=YYYY-MM-DD` checks one of our closes against
Nasdaq's free public endpoint — a source that shares no code, no vendor and no
convention with the chain. On matched days it agrees **to the cent**, mutual-fund NAVs
included:

```
VWELX  2026-08-27   ours 47.4300   ref 47.43   agree to 0.0000%
FCNTX  2026-08-27   ours 27.0300   ref 27.03   agree to 0.0000%
VOO    2026-08-28   ours 707.2400  ref 707.24  agree to 0.0000%
```

It has exactly **one automated caller**, and it is worth naming precisely because a
cross-check with no defined trigger is decoration. When a mutual-fund order has sat
unpriced for the whole `NAV_POLL_GIVE_UP_HOURS` window, the engine is about to throw
away a trade the user asked for — and two very different things could be true: the fund
published no NAV, or our providers cannot see one that exists. Only an independent
source separates them. If the NAV is confirmed, the order is **held** (up to
`NAV_HOLD_MAX_DAYS`) and the provider failure is logged as an error; if neither source
has it, the rejection is made with confidence and says so.

Otherwise it is a manual diagnostic. It is deliberately **not** on a schedule and **not**
in the ordinary fill path — that would add an upstream request to answer a question
nobody asked.

Nasdaq is deliberately *not* a price source, for three reasons: its history stops at
about seven years (this platform prices lots back to 2010), it publishes raw prices with
no splits endpoint to restate them, and inferring splits from steps in a mutual-fund NAV
series is guessing — a large year-end distribution looks like a small split.


## Reference data (seeded, not fetched)

| Data | Source | Where |
|---|---|---|
| Tradable universe, expense ratios, prospectus links | Hand-curated list, linked to [SEC EDGAR](https://www.sec.gov/edgar) 485BPOS filings | `backend/app/init_db.py` |
| IRA contribution limits and catch-up amounts | IRS annual figures (2024–2026), with later years auto-projected and replaced when official numbers land | `backend/app/init_db.py`, `app/services/irs.py` |
| NYSE trading calendar and holidays | Computed from exchange rules (fixed dates with weekend observance, floating Mondays, Good Friday via computus) plus known one-off closures | `app/services/market_calendar.py` |
| Settlement fund (VMFXX) yield history | Built-in rate history, overridable with `SETTLEMENT_YIELD_ANNUAL` | `app/services/settlement.py` |

Symbols outside the seeded universe are auto-registered on first use by validating them
against Yahoo's metadata, so the tradable universe is a starting point, not a limit.

## Caching and the upstream budget

The providers above are rate-limited by their operators, and Yahoo in particular never
agreed to serve you at all — so requests are cached and capped rather than issued on
demand.

| Response | Setting | Default |
|---|---|---|
| Quotes | `QUOTE_CACHE_SECONDS` | 30s (minimum 5) |
| Daily candles | `HISTORY_CACHE_SECONDS` | 1h |
| Dividend calendars | `DIVIDEND_CACHE_SECONDS` | 25h (deliberately longer than the daily sweep) |
| Symbol search | fixed | 5m |

`MARKET_UPSTREAM_PER_MINUTE` (default 120) caps outbound provider calls across **every**
process — API, worker and beat share one Redis bucket, because what is being protected
is the upstream's view of the deployment, not any single container. When the budget is
spent, a caller serves the last known price if it has one and fails cleanly if it does
not; it never queues. Set it to `0` to disable the cap.

## Nothing is fetched unless something needs it

Every upstream request traces back to one of three causes, and the third happens twelve
times a year:

1. **Data is needed to execute something** — an order is due, a NAV must be read, a
   contract is expiring, a backdated fill needs that day's close.
2. **A user is looking** — a page that shows prices was opened.
3. **A statement is being rendered** — on the 1st, for the month that just closed. This
   is the only clock-driven fetch left in the system, and it exists because a statement
   is a point-in-time document the archive is expected to hold whether or not anyone
   asked for it that morning.

An idle deployment (nobody signed in, nothing due, markets shut) makes **zero** provider
requests, indefinitely. The scheduled tasks still run every minute; they return without
touching a provider when there is no work:

| Task | Interval | Reaches a provider only when |
|---|---|---|
| Recurring investments | 60s | a rule's `next_run_at` has passed |
| Scheduled orders | 60s | an order is `SCHEDULED` and its time has come |
| Limit orders | 60s | the market is open **and** an order is resting |
| Order expiry | 5m | never — no market data involved |
| Settlement dividends | daily | never — $1.00 NAV, built-in yield history |
| Option expirations | 10m | a contract has reached its expiry |
| IRS limits, scenario purge | daily | never — no market data involved |
| Statements | monthly | always, on the 1st — the one clock-driven fetch (see above) |

Split application and dividend reconciliation are **not** on that list, and not on a
schedule. It is a pure
function of the transaction history and the ex-date calendar, so it returns the same
answer whenever it runs — which means it never has to run on the day of the ex-date,
only before something depends on the result. It is triggered by the only two things that
do: an order about to draw on the account's cash, and a user opening their portfolio.
Splits run first at those same moments, because historical dividend amounts are quoted
on the post-split basis — crediting them against pre-split share counts would understate
every distribution by exactly the split ratio.
Both are throttled to once per account per day (ex-dates have day granularity, so
nothing finer can discover anything), work is grouped by **security** rather than by
holding, and holdings that have been fully exited are skipped entirely.

## Live pricing in the UI

Every view that shows a price, a gain or an account total — dashboard, accounts, account
detail, the trade ticket and the options chain — re-prices itself automatically. The
cadence is decided by the **server**, from the trading calendar, and handed to the
browser:

| Regime | When | Cadence |
|---|---|---|
| `open` | market is trading | `MARKET_REFRESH_SECONDS` (default 60s) |
| `nav` | 4 hours after the close | 10× that (10 min) — fund NAVs are still posting, so portfolio values still move |
| `closed` | everything else | **0 — nothing is fetched** |

The NYSE is shut for 81% of the week, and a closed market's prices cannot change, so
polling through that would be exactly the request whose answer is already known.
Refreshing also pauses while a browser tab is hidden and fires immediately when it is
looked at again. Each of those pages carries the same market-status widget — *"Market
open · closes in 3h 12m · updated 2:31 PM"*, *"Market closed · opens Mon, 9:30 AM"* — so
a number on screen never has to be taken on trust, and the countdown is quoted against
the server's clock rather than the browser's.

There is no manual refresh control, because there is no moment it would help: while the
market is open the page re-prices on the cadence above and again the instant the tab is
looked at, and while it is shut there is nothing new to fetch. The one exception is a
deployment that has set `MARKET_REFRESH_SECONDS=0` — there, and only there, the widget
offers a **Refresh** link, because nothing else would ever move the numbers.

This does **not** scale with the number of viewers. Quotes come from one shared
server-side cache, so forty people watching the same holding cost one upstream request,
not forty; the ceiling is `tickers held / QUOTE_CACHE_SECONDS` regardless of audience.
Set `MARKET_REFRESH_SECONDS=0` to turn auto-refresh off entirely (1–14 is refused).


## Mutual fund NAVs

Funds are **forward-priced**, like the real thing: an order placed before the 4:00 PM ET
cutoff is promised that day's closing NAV, and an order placed after it gets the next
trading day's. The 4:00 PM cutoff decides *which* NAV you get — it is not when the NAV
is published. Most funds post between 5 and 8 PM ET, and some later.

So a fund order sits in `SCHEDULED` and the worker polls for the published close,
force-refreshing past the candle cache. `NAV_POLL_INTERVAL_SECONDS` (default 300) is the
shortest gap between two real re-fetches for the same fund and day — a NAV publishes
once and then stops changing, so asking more often spends the upstream budget for
nothing. `NAV_POLL_GIVE_UP_HOURS` (default 30) is when the order is rejected instead.

