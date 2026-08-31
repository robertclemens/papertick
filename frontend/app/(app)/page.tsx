"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountReturnsT,
  AccountT,
  api,
  EMPTY_PERFORMANCE,
  OrderT,
  PerformanceT,
  PositionT,
  RANGE_LABEL,
  RANGES,
  RangeT,
  SummaryT,
  TransactionT,
} from "@/lib/api";
import { dateTime, expenseRatioPct, money, pct, shares, shortDate, signedMoney } from "@/lib/format";
import { AllocationDonut, AllocationGroupBy, PortfolioChart } from "@/components/charts";
import { Badge, Card, Empty, Spinner, Stat } from "@/components/ui";
import { aggregateByTicker } from "@/lib/positions";
import { useRangePref } from "@/lib/prefs";

function gainClass(v: string | number | null | undefined): string {
  const n = typeof v === "string" ? parseFloat(v) : (v ?? 0);
  return n >= 0 ? "text-(--status-good)" : "text-(--status-critical)";
}

const ALLOC_VIEWS: { key: AllocationGroupBy; label: string }[] = [
  { key: "holding", label: "Holdings" },
  { key: "category", label: "Type" },
  { key: "region", label: "Region" },
];

export default function DashboardPage() {
  const [summary, setSummary] = useState<SummaryT | null>(null);
  const [positions, setPositions] = useState<PositionT[] | null>(null);
  const [perf, setPerf] = useState<PerformanceT | null>(null);
  const [txns, setTxns] = useState<TransactionT[] | null>(null);
  const [openOrders, setOpenOrders] = useState<OrderT[] | null>(null);
  const [range, setRange] = useRangePref();
  const [returns, setReturns] = useState<AccountReturnsT | null>(null);
  const [alloc, setAlloc] = useState<AllocationGroupBy>("holding");
  const [allAccounts, setAllAccounts] = useState<AccountT[]>([]);
  const [accountFilter, setAccountFilter] = useState("");

  const scope = accountFilter ? `account_id=${accountFilter}` : "";
  const amp = accountFilter ? "&" : "";

  useEffect(() => {
    api<AccountT[]>("/accounts").then(setAllAccounts).catch(() => {});
  }, []);

  useEffect(() => {
    setSummary(null);
    setPositions(null);
    setTxns(null);
    api<SummaryT>(`/portfolio/summary?${scope}`).then(setSummary).catch(() => setSummary(null));
    api<PositionT[]>(`/portfolio/positions?${scope}`).then(setPositions).catch(() => setPositions([]));
    // sort=effective: a past-dated fill belongs at its own date, not at the top
    // of the feed because it was entered today
    api<TransactionT[]>(`/transactions?limit=8&sort=effective${amp}${scope}`)
      .then(setTxns).catch(() => setTxns([]));
    Promise.all([
      api<OrderT[]>(`/orders?status=SCHEDULED&limit=50${amp}${scope}`),
      api<OrderT[]>(`/orders?status=PENDING&limit=50${amp}${scope}`),
    ])
      .then(([s, p]) => setOpenOrders([...s, ...p]))
      .catch(() => setOpenOrders([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountFilter]);

  useEffect(() => {
    if (!range) return;   // wait for the user's preferred window
    setPerf(null);
    api<PerformanceT>(`/portfolio/performance?range=${range}${amp}${scope}`)
      .then(setPerf)
      .catch(() => setPerf(EMPTY_PERFORMANCE));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, accountFilter]);

  // the account list carries its own per-account returns for the same window
  useEffect(() => {
    if (!range) return;
    setReturns(null);
    api<AccountReturnsT>(`/portfolio/returns?range=${range}`).then(setReturns).catch(() => {});
  }, [range]);

  // the table is a portfolio view: one row per symbol, summed across accounts
  const holdings = useMemo(() => aggregateByTicker(positions ?? []), [positions]);

  const unreal = summary ? parseFloat(summary.unrealized_gains) : 0;
  const taxableRealized = summary ? parseFloat(summary.realized_gains_taxable) : 0;
  const sheltered = summary ? parseFloat(summary.realized_gains_sheltered) : 0;

  return (
    <div className="space-y-6">
      <header className="flex w-full flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="mt-1 text-sm text-slate-400">Your simulated wealth at a glance.</p>
        </div>
        <div className="flex min-w-0 max-w-full items-center gap-2">
          {/* fixed width: a select sized w-auto grows to its longest option and
              would push this group past the cards below */}
          <select
            aria-label="Account scope"
            className="input h-10 w-56 max-w-full shrink-0 pr-8"
            value={accountFilter}
            onChange={(e) => setAccountFilter(e.target.value)}
          >
            <option value="">All accounts (aggregated)</option>
            {allAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]}
              </option>
            ))}
          </select>
          <Link href="/trade" className="btn-primary h-10 shrink-0 whitespace-nowrap">
            New trade
          </Link>
        </div>
      </header>

      {!summary ? (
        <Spinner />
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Total value" value={money(summary.total_value)} />
            <Stat
              label="Available to trade"
              value={money(summary.available_to_trade)}
              delta={
                parseFloat(summary.committed_cash) > 0 || parseFloat(summary.reserved_cash) > 0
                  ? `of ${money(summary.cash)} in settlement`
                  : undefined
              }
              deltaGood={null}
            />
            <Stat
              label="Unrealized gains"
              value={money(summary.unrealized_gains)}
              delta={summary.cost_basis !== "0.00" && parseFloat(summary.cost_basis) > 0
                ? pct((unreal / parseFloat(summary.cost_basis)) * 100)
                : undefined}
              deltaGood={unreal === 0 ? null : unreal > 0}
            />
            <Stat
              label="Realized gains"
              value={money(summary.realized_gains)}
              // A sale inside an IRA realizes a gain on paper but is not a
              // taxable event, so say which part of this the IRS ever sees.
              delta={
                sheltered !== 0
                  ? taxableRealized === 0
                    ? "all inside IRAs — not taxable"
                    : `${money(summary.realized_gains_taxable)} taxable · ${money(summary.realized_gains_sheltered)} in IRAs`
                  : undefined
              }
              deltaGood={null}
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card
              title="Performance"
              className="lg:col-span-2"
              action={
                <div className="flex gap-1" role="group" aria-label="Date range">
                  {RANGES.map((r) => (
                    <button
                      key={r}
                      onClick={() => setRange(r)}
                      className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${
                        range === r
                          ? "bg-slate-700 text-slate-100"
                          : "text-slate-400 hover:bg-slate-800"
                      }`}
                    >
                      {r.toUpperCase()}
                    </button>
                  ))}
                </div>
              }
            >
              {perf && perf.series.length > 0 && (
                <div className="mb-4 grid gap-4 border-b border-slate-800 pb-4 sm:grid-cols-3">
                  <div>
                    <div className="text-xs text-slate-400">Investment returns</div>
                    <div className={`mt-0.5 text-xl font-semibold tabular-nums ${gainClass(perf.investment_returns)}`}>
                      {signedMoney(perf.investment_returns)}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-slate-400">
                      Rate of return{perf.rate_of_return_annualized && " (annualized)"}
                    </div>
                    <div className={`mt-0.5 text-xl font-semibold tabular-nums ${
                      (perf.rate_of_return_pct ?? 0) >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
                    }`}>
                      {pct(perf.rate_of_return_pct)}
                    </div>
                  </div>
                  <dl className="space-y-1 text-xs">
                    <div className="flex justify-between gap-3">
                      <dt className="text-slate-400">Beginning balance</dt>
                      <dd className="tabular-nums text-slate-300">{money(perf.beginning_balance)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-slate-400">Deposits &amp; withdrawals</dt>
                      <dd className="tabular-nums text-slate-300">{signedMoney(perf.net_cash_flow)}</dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-slate-400">Ending balance</dt>
                      <dd className="tabular-nums text-slate-300">{money(perf.ending_balance)}</dd>
                    </div>
                  </dl>
                </div>
              )}
              {perf && perf.period_start && (
                <p className="mb-2 text-xs text-slate-500">
                  Date range: {shortDate(perf.period_start)} – {shortDate(perf.period_end)}
                  {range && <>{" · "}{RANGE_LABEL[range]}</>}
                </p>
              )}
              {!perf ? <Spinner /> : <PortfolioChart perf={perf} />}
              {perf && perf.series.length > 1 && (
                <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 border-t border-slate-800 pt-3 text-sm">
                  <div>
                    <span className="text-slate-400">Time-weighted return </span>
                    <span className={`font-medium ${
                      (perf.twr_pct ?? 0) >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
                    }`}>{pct(perf.twr_pct)}</span>
                  </div>
                  <div>
                    <span className="text-slate-400">IRR (annualized) </span>
                    <span className={`font-medium ${
                      (perf.irr_pct ?? 0) >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
                    }`}>{pct(perf.irr_pct)}</span>
                  </div>
                  <div>
                    <span className="text-slate-400">Dividends &amp; income </span>
                    <span className="font-medium text-slate-200">{money(perf.dividends)}</span>
                  </div>
                  <span className="text-xs text-slate-500">
                    all figures for the selected timeframe
                  </span>
                </div>
              )}
            </Card>
            <Card
              title="Allocation"
              action={
                <div className="flex gap-1" role="group" aria-label="Allocation view">
                  {ALLOC_VIEWS.map((v) => (
                    <button
                      key={v.key}
                      onClick={() => setAlloc(v.key)}
                      className={`rounded-md px-2 py-1 text-xs font-medium transition ${
                        alloc === v.key ? "bg-slate-700 text-slate-100" : "text-slate-400 hover:bg-slate-800"
                      }`}
                    >
                      {v.label}
                    </button>
                  ))}
                </div>
              }
            >
              {!positions ? (
                <Spinner />
              ) : (
                <AllocationDonut positions={positions} cash={parseFloat(summary.cash)} groupBy={alloc} />
              )}
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            {/* portfolio-level: one row per symbol, not per account */}
            <Card title="Holdings" className="lg:col-span-2">
              {!positions ? (
                <Spinner />
              ) : holdings.length === 0 ? (
                <Empty>No holdings yet — place your first trade.</Empty>
              ) : (
                <div className="overflow-x-auto">
                  <table className="table-base">
                    <thead>
                      <tr>
                        <th>Ticker</th><th>Shares</th><th>Avg cost</th><th>Price</th>
                        <th>Value</th><th>Gains</th>
                      </tr>
                    </thead>
                    <tbody>
                      {holdings.map((h) => (
                        <tr key={h.ticker}>
                          <td>
                            <div className="font-medium text-slate-100">{h.ticker}</div>
                            <div className="text-xs text-slate-500">
                              {h.name}
                              {expenseRatioPct(h.expense_ratio) && ` · ER ${expenseRatioPct(h.expense_ratio)}`}
                              {h.account_ids.length > 1 && (
                                <>
                                  {" · "}
                                  <span
                                    className="text-slate-400"
                                    title={h.account_ids
                                      .map((id) => allAccounts.find((a) => a.id === id)?.name ?? id)
                                      .join(", ")}
                                  >
                                    held in {h.account_ids.length} accounts
                                  </span>
                                </>
                              )}
                            </div>
                          </td>
                          <td>{shares(h.shares)}</td>
                          <td>{money(h.average_cost)}</td>
                          <td>{money(h.price)}</td>
                          <td>{money(h.market_value)}</td>
                          <td className={h.unrealized_gains >= 0 ? "text-(--status-good)" : "text-(--status-critical)"}>
                            {signedMoney(h.unrealized_gains)}{" "}
                            <span className="text-xs">({pct(h.unrealized_gains_pct)})</span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            <Card
              title="Accounts"
              action={<Link className="text-xs text-emerald-400 hover:text-emerald-300" href="/accounts">Manage</Link>}
            >
              {summary.accounts.length === 0 ? (
                <Empty>
                  No accounts yet.{" "}
                  <Link href="/accounts" className="text-emerald-400">Open one</Link> to get started.
                </Empty>
              ) : (
                <>
                  <p className="mb-2 text-xs text-slate-500">
                    Balance and returns over {range ? RANGE_LABEL[range].toLowerCase() : "…"}{" "}
                    (set by the Performance timeframe).
                  </p>
                  <ul className="space-y-2">
                    {summary.accounts.map((a) => {
                      const r = returns?.accounts.find((x) => x.account_id === a.id);
                      return (
                        <li key={a.id}>
                          <Link
                            href={`/accounts/${a.id}`}
                            className="block rounded-lg border border-slate-800 px-3 py-2.5 transition hover:border-slate-700 hover:bg-slate-800/50"
                          >
                            <div className="flex items-baseline justify-between gap-3">
                              <span className="truncate text-sm font-medium text-slate-100">{a.name}</span>
                              <span className="shrink-0 text-sm font-medium tabular-nums text-slate-100">
                                {money(r ? r.balance : a.settlement_balance)}
                              </span>
                            </div>
                            <div className="text-xs text-slate-500">{ACCOUNT_TYPE_LABEL[a.account_type]}</div>
                            {r && (
                              <dl className="mt-1.5 space-y-0.5 text-xs">
                                <div className="flex justify-between gap-3">
                                  <dt className="text-slate-500">Investment returns</dt>
                                  <dd className={`tabular-nums ${gainClass(r.investment_returns)}`}>
                                    {signedMoney(r.investment_returns)}
                                  </dd>
                                </div>
                                <div className="flex justify-between gap-3">
                                  <dt className="text-slate-500">
                                    Rate of return{r.rate_of_return_annualized && " (ann.)"}
                                  </dt>
                                  <dd className={`tabular-nums ${
                                    (r.rate_of_return_pct ?? 0) >= 0
                                      ? "text-(--status-good)" : "text-(--status-critical)"
                                  }`}>{pct(r.rate_of_return_pct)}</dd>
                                </div>
                              </dl>
                            )}
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                </>
              )}
            </Card>
          </div>

          {openOrders && openOrders.length > 0 && (
            <Card
              title={`Open orders (${openOrders.length})`}
              action={
                <span className="text-xs text-slate-400">
                  {money(summary.committed_cash)} committed from settlement
                </span>
              }
            >
              <div className="overflow-x-auto">
                <table className="table-base">
                  <thead>
                    <tr>
                      <th>Placed</th><th>Side</th><th>Ticker</th><th>Type</th>
                      <th>Amount</th><th>Status</th><th>Expected</th>
                    </tr>
                  </thead>
                  <tbody>
                    {openOrders.map((o) => (
                      <tr key={o.id}>
                        <td className="whitespace-nowrap">{shortDate(o.created_at)}</td>
                        <td><Badge value={o.side} /></td>
                        <td className="font-medium">{o.ticker}</td>
                        <td className="text-xs text-slate-400">
                          {o.order_type}
                          {o.limit_price && ` @ ${money(o.limit_price)}`}
                          {o.source === "RECURRING" && " · auto"}
                        </td>
                        <td>
                          {o.quantity_type === "DOLLARS" ? money(o.quantity) : `${shares(o.quantity)} sh`}
                        </td>
                        <td><Badge value={o.status} /></td>
                        <td className="text-xs text-slate-400">
                          {o.nav_date
                            ? `NAV ${shortDate(o.nav_date)}`
                            : o.scheduled_for
                              ? dateTime(o.scheduled_for)
                              : o.expires_at
                                ? `expires ${shortDate(o.expires_at)}`
                                : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-2 text-xs text-slate-500">
                Settlement money backing these orders is earmarked until they fill, are
                cancelled, or expire — it is not available to trade or withdraw.
              </p>
            </Card>
          )}

          <Card title="Recent activity">
            {!txns ? (
              <Spinner />
            ) : txns.length === 0 ? (
              <Empty>No transactions yet.</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="table-base">
                  <thead>
                    <tr><th>Date</th><th>Side</th><th>Ticker</th><th>Shares</th><th>Price</th><th>Amount</th><th>Realized gains</th></tr>
                  </thead>
                  <tbody>
                    {txns.map((t) => (
                      <tr key={t.id}>
                        <td>{shortDate(t.as_of)}</td>
                        <td><Badge value={t.side} /></td>
                        <td className="font-medium">{t.ticker}</td>
                        <td>{shares(t.shares_filled)}</td>
                        <td>{money(t.executed_price)}</td>
                        <td>{money(t.gross_amount)}</td>
                        <td className={
                          t.realized_gains == null ? "text-slate-500"
                          : parseFloat(t.realized_gains) >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
                        }>
                          {t.realized_gains == null ? "—" : signedMoney(t.realized_gains)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
