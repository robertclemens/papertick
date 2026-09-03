# The REST API

Everything the web UI can do, an agent or a shell script can do too — the UI is
just one client of this API. The always-current contract is the OpenAPI spec the
running app serves; this page is the orientation you need before reading it.

- Interactive: `GET /api/docs` (Swagger) or `/api/redoc`
- Raw spec: `GET /api/openapi.json`

Under `ENV=production` all three require an authenticated caller, so pass your
API key when fetching them.

## Base URL

The API is reached **through the frontend origin**, on the same host and scheme
the web UI is served on — not on a separate backend port. Compose never
publishes the backend at all.

```
http://localhost:3000/api/v1/...          # local
https://yourdomain.example/api/v1/...     # own hostname
https://domain.example/papertick/api/v1/...   # BASE_PATH deployment
```

## Authentication

Two ways in, and they are not equivalent.

| Credential | Header | Scopes | Good for |
|---|---|---|---|
| API key | `Authorization: Bearer ptk_…` | `read`, `trade` | agents, scripts, cron |
| Session cookie | set at login by the UI | `read`, `trade`, `manage` | the browser |

Create a key under **Settings → API keys**. The plaintext is shown exactly once
and stored only as a SHA-256 hash; it is revocable from the same page.

`read` and `trade` between them cover querying everything and depositing,
withdrawing, trading and scheduling. Administration — creating or editing
accounts, cost-basis elections, scenarios, API keys, MFA and passkeys — needs
`manage`, which only an interactive session holds. In practice: set the accounts
up in the UI, then let the agent work inside them.

## Scenarios

Every account, order and statement belongs to a scenario — an independent
"what-if" track. Requests without a scenario use your default one. Every
response carries `X-Scenario-Id` and `X-Scenario-Name`, so a caller always knows
which track answered.

Pin one explicitly with a header or a query parameter:

```bash
curl -H "X-Scenario-Id: <uuid>" ...
curl "$PT_URL/api/v1/portfolio/summary?scenario_id=<uuid>"
```

## Worked examples

```bash
export PT_KEY=ptk_...
export PT_URL=http://localhost:3000

# what do I hold?
curl -s -H "Authorization: Bearer $PT_KEY" $PT_URL/api/v1/portfolio/summary

# list accounts, to get the account_id the calls below need
curl -s -H "Authorization: Bearer $PT_KEY" $PT_URL/api/v1/accounts
```

```bash
# put $2,000 into an account (in an IRA this is a contribution, and is
# checked against the annual limit)
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"amount":"2000","tax_year":2026}' \
  $PT_URL/api/v1/accounts/<uuid>/deposit

# buy $500 of VOO at the current price
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"VOO","side":"BUY","quantity_type":"DOLLARS","quantity":"500"}' \
  $PT_URL/api/v1/orders

# a resting limit order, good till canceled for 90 days
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"AAPL","side":"BUY","order_type":"LIMIT",
       "quantity_type":"SHARES","quantity":"10","limit_price":"180.00",
       "time_in_force":"GTC_90"}' \
  $PT_URL/api/v1/orders

# backtest: pretend you bought $10k of AAPL three years ago.
# Needs past-dated trades enabled on the active scenario.
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"AAPL","side":"BUY","quantity_type":"DOLLARS","quantity":"10000","as_of":"2023-08-29"}' \
  $PT_URL/api/v1/orders

# $500 of VOO on the 1st of every month, forever
curl -s -X POST -H "Authorization: Bearer $PT_KEY" -H "Content-Type: application/json" \
  -d '{"account_id":"<uuid>","ticker":"VOO","amount":"500","cadence":"MONTHLY","day_of_month":1}' \
  $PT_URL/api/v1/schedules
```

```bash
# a year of realized gains, dividend income and contributions
curl -s -H "Authorization: Bearer $PT_KEY" "$PT_URL/api/v1/tax/report?year=2026"

# month-by-month performance, every row balancing to the cent
curl -s -H "Authorization: Bearer $PT_KEY" $PT_URL/api/v1/portfolio/performance/monthly

# download two years of transactions as a spreadsheet
curl -s -H "Authorization: Bearer $PT_KEY" \
  "$PT_URL/api/v1/export/transactions.xlsx?range=3y" -o transactions.xlsx
```

## The endpoint map

| Area | Routes | Scope |
|---|---|---|
| Accounts, deposits, withdrawals, Roth conversions | `/accounts…` | `read` / `trade` |
| Contribution room and IRS limits | `/irs/status`, `/irs/allowed-years` | `read` |
| Cost-basis elections | `/accounts/{id}/cost-basis` | `manage` |
| Orders, transactions, exchanges | `/orders`, `/transactions` | `read` / `trade` |
| Recurring rules | `/schedules…` | `read` / `trade` |
| Options chains, orders, positions | `/options…` | `read` / `trade` |
| Holdings, lots, returns, performance, dividends | `/portfolio…` | `read` |
| Tax report | `/tax…` | `read` |
| Quotes, history, search, market status, providers | `/market…` | `read` |
| Monthly and year-end PDFs | `/statements…` | `read` |
| CSV / Excel history exports | `/export/{dataset}.{csv,xlsx}` | `read` |
| Scenario create, copy, export, import, delete | `/scenarios…` | `manage` |
| Login, MFA, passkeys, password recovery, security log, profile, API keys | `/auth…`, `/api-keys…` | session |

All under `/api/v1`. `GET /healthz` sits outside it and needs no credential.

## Behaviour worth knowing before you script against it

- **Money is a string.** Amounts and share counts are decimals; send them as
  strings so a JSON float cannot round them. Shares go to 6 decimal places.
- **A fill is not always immediate.** Outside market hours a market order comes
  back `SCHEDULED` and fills at the next open; a mutual fund order fills at that
  day's closing NAV once the fund publishes it, hours after the close. Poll
  `GET /api/v1/orders/{id}` rather than assuming the response is final.
- **Buying power is not the same as cash.** It excludes cash committed to open
  orders and collateral reserved by short puts.
- **Errors are actionable.** A blocked deposit says which limit it hit and by how
  much; a rejected order says why. Read `detail` rather than only the status code.
- **Rate limits apply to keys too.** Auth, trading, search and export endpoints
  are limited and answer `429` when you outrun them. Back off and retry; there is
  no `Retry-After` header to read.
