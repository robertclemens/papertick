# Features in detail

The full inventory. The README summarises this in a dozen lines; everything
below is what those lines actually mean.


- **Real-market emulation** — NYSE calendar and hours (9:30–16:00 ET, DST-correct,
  computed holidays): market orders placed off-hours queue for the next open;
  mutual funds are forward-priced at the daily closing NAV (4 PM ET cutoff), never
  intraday; limit orders only work while the market trades. Set
  `ENFORCE_MARKET_HOURS=false` for an always-open sandbox.
- **Settlement fund** — uninvested cash is not a bare balance: it sits in a federal
  money market fund modeled on VMFXX at a stable $1.00 NAV. Deposits and sale
  proceeds sweep in, purchases and withdrawals sweep out, and the balance accrues
  the fund's dividend daily, paid on the last day of each month. No settlement or
  clearing holds are modeled — swept-in cash is available immediately.
- **Stock splits** — a split is the one corporate action that changes a position
  with no order behind it, and ignoring one fails *silently*: the price series is
  restated onto the new basis while the share count is not, so the holding loses
  exactly the value of the split (NVDA's 10:1 would take a $100,000 position to
  $10,000 overnight). Splits are detected from the provider's own event feed and
  applied to open lots — shares multiply, per-share basis divides, **total basis and
  acquisition dates are preserved**, so realized gains and holding periods are
  untouched. Applied at most once per account/security/ex-date, enforced by a
  database key rather than a flag. Backdated fills are deliberately excluded: they
  are priced from the already-restated series, so their share count is post-split
  from the start.
- **Dividends** — real dividend history is credited for shares held on each
  ex-date, including full backfill for backdated (backtest) positions, and flows
  into balances, performance, and the tax report. Reconciliation is idempotent and
  re-runs every few hours.
- **Tax lots & taxable income** — full lot tracking (fees in basis, backtests earn
  their real holding period), every sale split into short-/long-term gains, and a
  per-year tax report (gains, dividend income, IRA contributions by designation,
  withdrawals) with CSV export.
- **Cost-basis elections** (taxable brokerage only — IRAs have no capital-gains
  treatment and always use FIFO) — FIFO (default), LIFO, HIFO, MinTax (losses
  first), Average Cost (mutual funds only, per IRS; re-bases remaining shares),
  and Specific Lot ID with a per-sale lot picker. Set per account, per fund
  (Vanguard-style), or overridden on any single sale. Methods stay changeable
  and apply to future sales; average cost locks the averaged basis of existing
  shares once a sale has used it (IRS §1.1012-1(e)), and SpecID with no lots
  named at sale time falls back to FIFO. The method actually used is recorded
  on every order.
- **Options** — calls & puts on any underlying: long options, covered calls, and
  cash-secured puts (collateral reserved from buying power). Black-Scholes chains
  priced off the live underlying, weekly/monthly/LEAPS listings, automatic
  ITM exercise & assignment at expiration with IRS-style basis/proceeds
  adjustments, and plain-English explanations of every contract's rights,
  obligations, max loss and breakeven.
- **Statements** — monthly PDFs generated on the 1st for each completed month plus
  year-end statements with the full tax summary; letter-size, PaperTick masthead,
  archived immutably and downloadable from the Statements page.
- **IRS limit auto-maintenance** — a daily task carries the latest official
  contribution limits forward into new tax years as "projected" (flagged in the
  API/UI) until official figures are seeded, so limits never go missing at
  year-roll.
- **Open symbol universe** — any US-listed USD equity/ETF/mutual fund validated by
  the live data source is auto-registered on first use; unknown symbols are
  rejected. A curated seed carries category/region/expense-ratio/prospectus data.
  Search accepts **company or fund names** as well as tickers ("apple" → AAPL,
  "contrafund" → FCNTX), filtered to US listings.
- **Account buckets** — Taxable, Roth IRA, Traditional IRA, Rollover IRA per user.
- **Scenarios** — every account, order and statement lives in an independent
  "what-if" track (`X-Scenario-Id`); copy the current one, try a different
  strategy, and delete it without touching real data. A copy takes one of two
  shapes, and the choice is explicit at creation:
  **position** re-prices the balances and holdings at today's market and leaves
  the past behind, so the new track starts flat and measures what happens from
  here; **full** duplicates the source exactly — every order, transaction, tax
  lot, dividend, contribution and auto-invest rule — so returns and history
  carry over. Deletions are soft (30-day recovery window by default) and a
  deleted scenario is frozen: it stops trading, accruing dividends, and earning,
  without being purged.
- **IRS rule engine** — annual IRA limits shared across all IRAs (with age-50+ catch-up
  from your date of birth), prior-year designation between Jan 1 and Tax Day,
  rollovers exempt, over-limit deposits blocked with actionable errors.
  Limits are data (`irs_limits` table), seeded for 2024–2026.
- **Trading engine** — market & limit orders, buy/sell by dollars or shares
  (fractional to 6 dp), flat fee, average-cost basis, realized/unrealized gains.
  Slippage is drawn per fill from a configurable window rather than applied as a
  fixed number, seeded from the order id so backtests stay reproducible (see
  [Simulated trading costs](accounting.md#simulated-trading-costs)). All fills run inside DB
  transactions with row locks.
- **Backtesting** — place an order `as_of` a past date ("pretend I invested then");
  it fills at that day's close and flows into today's balances and performance.
  Off by default and enabled **per scenario**, not per deployment: one track can
  backtest freely while another stays a clean record. Every such fill is stored
  flagged and shown as past-dated wherever its numbers appear, so a return that
  was produced with hindsight always says so.
- **Scheduled & recurring investing** — one-off future orders and recurring rules
  (daily, weekly, biweekly, monthly, quarterly, annually) executed by the Celery
  worker; rules can be edited (amount, symbol, cadence) with changes applying to
  future runs only, and failures are recorded on the order and counted on the rule.
  An optional "fund to my contribution limit" mode divides remaining IRA room
  across the runs left in the year.
- **Outage catch-up** — recurring buys the worker slept through are not lost and
  not collapsed. On restart, every missed occurrence is replayed at the close
  actually printed on **its own day**, with the order and transaction stamped to
  the time they were due, so the ledger ends up where it would have been had
  nothing stopped. Bounded by `MAX_CATCHUP_DAYS` (default 30) so restoring an
  old backup cannot fire a year of trades in one tick; set
  `CATCHUP_MISSED_RUNS=false` to fire a single buy at today's price instead.
- **Buying power & external funding** — available-to-trade is cash minus
  short-put collateral minus cash already committed to open orders, so the same
  dollars can never back two pending trades; resting sell orders reserve their
  shares the same way. Committed cash is also excluded from withdrawals and is
  surfaced on the dashboard, account pages, and trade ticket alongside the open
  orders holding it. Every purchase is paid for out of the settlement fund and
  pulls in whatever it is short, recorded as a cash transfer — the platform has
  no view of what you hold elsewhere, so it reads the order as a statement that
  the money exists. The two refusals left are legal ones: in an IRA transfers
  count as contributions and stop at the annual limit, and a Rollover IRA takes
  no regular contribution at all, so a short purchase there is rejected.
- **Time in force** — limit orders carry an expiry (Day, or good-till-canceled
  at 30/60/90/180 days or 1 year, defaulting to 60 days). A worker sweep lapses
  them at expiry and releases the cash or shares they were holding.
- **Metrics** — portfolio value series rebuilt from the ledger, time-weighted return,
  annualized IRR (XIRR), cost basis, fees, net deposits.
- **Roth conversions** — move cash or whole positions from a Traditional or
  Rollover IRA into a Roth, with the tax split shown before you commit. Modelled
  properly: Form 8606 pro-rata, Roth withdrawal ordering, both five-year clocks,
  and the 10% early-distribution penalty (see
  [IRA tax mechanics](accounting.md#ira-tax-mechanics)).
- **Month-by-month performance** — a Performance page giving each month its own
  row: beginning balance, deposits and withdrawals, market gain/loss, income,
  what the portfolio earned, the running total since inception, and the ending
  balance. Every row balances exactly —
  `ending = beginning + flows + market + income` — and clicking one opens the
  deposits, fills and dividends that produced it. Aggregated across accounts by
  default, filterable to one account type or one account. The dashboard carries
  a compact strip for the month in progress.
- **History & activity** — orders, transactions and dividends in one place, each
  row naming the account it happened in, filterable to a single bucket or across
  all of them, over any window.
- **Exports** — every History view downloads as CSV or Excel over the window and
  account filter the table is showing; a whole scenario exports to a JSON file
  signed with `SECRET_KEY`, so a hand-edited file is rejected on import, and
  imports back as an exact duplicate (statements excluded — they re-render from
  the ledger).
- **Agentic API** — every feature is reachable over REST, documented by a live
  OpenAPI spec. Scoped API keys (`read`, `trade`) via `Authorization: Bearer ptk_…`
  cover reading and trading; creating accounts, managing scenarios and changing
  credentials need a signed-in session (see [the API guide](api.md)).

