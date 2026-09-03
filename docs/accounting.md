# Accounting, tax and performance

How PaperTick tracks basis, applies IRS rules, and computes the numbers on the
performance pages. None of this is tax advice — it is a simulation that tries
to follow the real rules closely enough to be worth reasoning about.

## IRA tax mechanics

A Roth conversion is the only conversion there is — Traditional or Rollover into
Roth — and the refusals are part of the feature. Roth to Traditional is
impossible (recharacterising a conversion ended with the Tax Cuts and Jobs Act in
2018); taxable to IRA is a *contribution*, against the annual limit; IRA to
taxable is a *distribution*. Each is rejected with the reason, because someone
reaching for one of them has a real misconception.

A conversion has **no annual limit and no income cap**, so it never touches
contribution room. It is ordinary income to the extent it is pre-tax.

**Form 8606 pro-rata.** Money leaving the pre-tax side carries after-tax basis in
proportion, computed across *every* Traditional and Rollover IRA together. You
cannot isolate after-tax dollars by parking them in their own account: $5,000 of
basis in one IRA, against $50,000 held across all of them, makes a $5,000
conversion 90% taxable — not tax-free. This is the rule that decides whether a
backdoor Roth works, which is why basis is tracked at all.

Basis is **declared, not inferred**. Whether a contribution was deductible turns
on income and workplace-plan coverage the platform never sees, so a Traditional
or Rollover IRA deposit carries a "nondeductible" checkbox — exactly as the real
form does.

**Roth withdrawal ordering.** A non-qualified Roth distribution comes out in a
fixed order, and each layer is treated differently:

| Layer | Income tax | 10% penalty |
|---|---|---|
| Regular contributions | none | none |
| Conversions, oldest first | none (already taxed) | yes, if within *that conversion's* five years and under 59½ |
| Earnings | ordinary income | yes, under 59½ |

**Two five-year clocks**, answering different questions. The *account* clock runs
from January 1 of your first Roth contribution or conversion — one per person,
never reset — and decides whether earnings come out tax-free. Each *conversion*
starts its own, deciding whether that converted money escapes the 10% penalty.
Conflating them is the usual way to get this wrong.

**Not modelled**, and said so on the Taxes page rather than implied: required
minimum distributions, the individual exceptions to the 10% penalty (recorded as
an attestation instead — each needs facts the platform cannot see), state income
tax, and the net investment income tax.

## What each cash movement is called

Every dollar entering or leaving an account is one row with one kind, and the
kind decides which annual limit it consumes and which line it lands on in the
tax summary. Getting it wrong does not lose money — it misreports it.

| Kind | Consumes IRA room | On the Taxes page |
|---|---|---|
| `CONTRIBUTION` | yes, in its designated tax year | IRA contributions |
| `ROLLOVER` | no | Rollovers received |
| `OPENING_BALANCE` | no | Opening balances |
| `WITHDRAWAL` | — | Withdrawals |
| `CONVERSION` | no | Roth conversions |

`OPENING_BALANCE` is the value an account was carried in with when a scenario
was copied: its cash plus the market value of its holdings, so the new track's
performance starts at zero instead of inheriting gains earned elsewhere. It is
not a contribution — the money was already inside the wrapper — and it is not a
rollover, which is a specific reportable event. It used to be written as a
rollover, which added the whole value of every copied account to "rollovers
received", and put a rollover on taxable brokerage accounts, which cannot
receive one at all. Existing rows are reclassified on startup.

Two gates keep the distinction from eroding:

- A **taxable brokerage account cannot receive a rollover.** There is no such
  event for a non-tax-advantaged account.
- A **rollover into a Roth IRA** is accepted — Roth 401(k) money and Roth-to-Roth
  transfers are real — but says what it must be. Pre-tax money moved into a Roth
  is a *conversion*: ordinary income, and it starts its own five-year clock, so
  it has to be recorded as one.
- An **opening balance cannot be deposited** through the API. It is written by a
  scenario copy and nothing else; accepting one would let external money into an
  IRA outside the annual limit just by naming a different kind.

**Scenarios are scoped, everywhere.** Each scenario is an independent track with
its own contribution history, so the annual limit, the tax summary and the
birthdate-impact check are all computed within one scenario. Summing across them
is not a bigger number, it is a wrong one — two tracks holding the same imported
history would report double the contributions and double the rollovers.

## How performance is calculated

Every performance figure in the product — the dashboard chart, the windowed
summary, and the month-by-month table — comes off **one** replay of the ledger,
day by day, from first activity. They deliberately do not each compute their own
beginning balance: that is exactly how a table stops agreeing with the chart
printed above it.

The replay separates three things that a naive "value today minus value then"
would blur together:

| | | |
|---|---|---|
| **External flows** | deposits, withdrawals, rollovers | not performance |
| **Income** | dividends and settlement-fund interest | performance |
| **Market movement** | everything left over | performance |

An option premium is a trading result rather than income, so it stays in cash
and lands in market movement. A past-dated fill enters as an in-kind *external
flow* on its effective date, with the matching cash effect on the day it was
actually entered — so backdating moves money into the timeline without ever
being counted as a gain.

The **rate of return** is money-weighted, like Vanguard's: an annualized IRR
(XIRR) over a year or more, and Modified Dietz over shorter windows, where
annualizing would inflate it. Because it accounts for how much was invested and
for how long, it will differ from any individual holding's published return.

Rounding is applied before the residual, not after: market gain/loss is derived
from the already-rounded figures, so a row balances to the cent by construction
rather than by luck.


## Simulated trading costs

Costs are simulated rather than fetched, and the slippage model is configurable because
a single fixed number is the one thing real fills never look like.

| Setting | Default | Effect |
|---|---|---|
| `SLIPPAGE_MODEL` | `variable` | `variable` draws per fill; `fixed` applies `SLIPPAGE_BPS` flat |
| `SLIPPAGE_BPS` | 2 | The mode under `variable`, the exact value under `fixed` |
| `SLIPPAGE_BPS_MIN` | -1 | Window floor. Negative = a fill better than the quote |
| `SLIPPAGE_BPS_MAX` | 6 | Window ceiling |
| `TRADE_FEE_USD` | 0.00 | Flat per-order commission |
| `OPTION_FEE_PER_CONTRACT` | 0.65 | Per-contract options fee |

Under `variable`, each fill draws from a triangular distribution over
[`MIN`, `BPS`, `MAX`]: most fills land near the typical spread, the adverse tail is
longer than the favourable one, and a negative draw represents the price improvement
retail order flow genuinely receives. The draw is **seeded from the order id**, so
re-running a backtest reproduces its fills instead of inventing new ones, and a
pre-trade preview shows the mode rather than a sample that would move on every refresh.
Mutual funds are never slipped — they transact at NAV, which is not a quote you can miss.

