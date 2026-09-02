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

