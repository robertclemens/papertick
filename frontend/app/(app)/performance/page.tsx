"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  EMPTY_PERFORMANCE,
  MonthEventT,
  MonthPerformanceT,
  PerformanceT,
  RANGE_LABEL,
  RANGES,
  RangeT,
} from "@/lib/api";
import { money, monthLabel, pct, shortDate, signedMoney } from "@/lib/format";
import { PortfolioChart } from "@/components/charts";
import { BackdatedNote, Card, Empty, MarketStatus, Spinner } from "@/components/ui";
import { useRangePref } from "@/lib/prefs";
import { useMarketRefresh } from "@/lib/market-refresh";

/** How many months the table shows before "Show more". A year is the span most
 *  people actually reason about, and it keeps the first screen readable. */
const PAGE_MONTHS = 12;

/** Gains are green, losses red — the same pairing used everywhere else in the
 *  app. Exact zero is neither: colouring it would imply a direction it does not
 *  have. */
function toneOf(v: string): string {
  const n = parseFloat(v);
  if (!isFinite(n) || n === 0) return "text-slate-400";
  return n > 0 ? "text-(--status-good)" : "text-(--status-critical)";
}

function isCurrentMonth(month: string): boolean {
  const now = new Date();
  return month === `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

export default function PerformancePage() {
  const [accounts, setAccounts] = useState<AccountT[]>([]);
  // "" = every account · "type:ROTH_IRA" = one kind · "id:<uuid>" = one account
  const [scope, setScope] = useState("");
  const [months, setMonths] = useState<MonthPerformanceT[] | null>(null);
  const [limit, setLimit] = useState(PAGE_MONTHS);
  const [perf, setPerf] = useState<PerformanceT | null>(null);
  const [range, setRange] = useRangePref();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [events, setEvents] = useState<Record<string, MonthEventT[]>>({});

  const query = useMemo(() => {
    if (scope.startsWith("id:")) return `account_id=${scope.slice(3)}`;
    if (scope.startsWith("type:")) return `account_type=${scope.slice(5)}`;
    return "";
  }, [scope]);

  useEffect(() => {
    api<AccountT[]>("/accounts").then(setAccounts).catch(() => {});
  }, []);

  const loadMonths = useCallback(() => {
    // months=600 is "everything": the table pages client-side so a reader can
    // open the whole history without re-fetching, and 50 years is past the
    // point where a paper-trading ledger has anything to say
    api<{ months: MonthPerformanceT[] }>(`/portfolio/performance/monthly?months=600&${query}`)
      .then((r) => setMonths(r.months))
      .catch(() => setMonths([]));
  }, [query]);

  useEffect(() => {
    setMonths(null);
    setExpanded(null);
    setEvents({});
    setLimit(PAGE_MONTHS);
    loadMonths();
  }, [loadMonths]);

  useEffect(() => {
    if (!range) return;
    setPerf(null);
    api<PerformanceT>(`/portfolio/performance?range=${range}${query ? `&${query}` : ""}`)
      .then(setPerf)
      .catch(() => setPerf(EMPTY_PERFORMANCE));
  }, [range, query]);

  const { status: marketStatus, lastRefresh, refreshing, refreshNow } = useMarketRefresh(loadMonths);

  function toggle(month: string) {
    if (expanded === month) {
      setExpanded(null);
      return;
    }
    setExpanded(month);
    if (!events[month]) {
      api<MonthEventT[]>(`/portfolio/performance/monthly/${month}/events?${query}`)
        .then((rows) => setEvents((cur) => ({ ...cur, [month]: rows })))
        .catch(() => setEvents((cur) => ({ ...cur, [month]: [] })));
    }
  }

  const shown = months?.slice(0, limit) ?? [];
  const backdatedShown = shown.reduce((n, m) => n + m.backdated_fills, 0);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">Performance</h1>
          <p className="mt-1 text-sm text-slate-400">
            What your money did each month, with deposits separated from what the market gave back.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <MarketStatus status={marketStatus} lastRefresh={lastRefresh}
                          refreshing={refreshing} onRefresh={refreshNow} />
            <span className="mt-2"><BackdatedNote count={backdatedShown} /></span>
          </div>
        </div>
        <select
          aria-label="Account scope"
          className="input h-10 w-64 max-w-full shrink-0 pr-8"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        >
          <option value="">All accounts (aggregated)</option>
          <optgroup label="By account type">
            {Object.entries(ACCOUNT_TYPE_LABEL)
              .filter(([t]) => accounts.some((a) => a.account_type === t))
              .map(([t, label]) => (
                <option key={t} value={`type:${t}`}>{label}</option>
              ))}
          </optgroup>
          <optgroup label="One account">
            {accounts.map((a) => (
              <option key={a.id} value={`id:${a.id}`}>{a.name}</option>
            ))}
          </optgroup>
        </select>
      </header>

      <Card
        title="Portfolio value"
        action={
          <div className="flex flex-wrap gap-1" role="group" aria-label="Chart timeframe">
            {RANGES.map((r: RangeT) => (
              <button
                key={r}
                onClick={() => setRange(r)}
                title={RANGE_LABEL[r]}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${
                  range === r ? "bg-slate-700 text-slate-100" : "text-slate-400 hover:bg-slate-800"
                }`}
              >
                {r.toUpperCase()}
              </button>
            ))}
          </div>
        }
      >
        {perf && perf.period_start && (
          <p className="mb-2 text-xs text-slate-500">
            {shortDate(perf.period_start)} – {shortDate(perf.period_end)}
          </p>
        )}
        {!perf ? <Spinner /> : <PortfolioChart perf={perf} />}
        {perf && perf.series.length > 1 && (
          <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 border-t border-slate-800 pt-3 text-sm">
            <div>
              <span className="text-slate-400">
                Personal rate of return{perf.rate_of_return_annualized ? " (annualized)" : ""}{" "}
              </span>
              <span className={`font-medium ${toneOf(String(perf.rate_of_return_pct ?? 0))}`}>
                {pct(perf.rate_of_return_pct)}
              </span>
            </div>
            <div>
              <span className="text-slate-400">Time-weighted return </span>
              <span className={`font-medium ${toneOf(String(perf.twr_pct ?? 0))}`}>
                {pct(perf.twr_pct)}
              </span>
            </div>
            <span className="text-xs text-slate-500">for the selected timeframe</span>
          </div>
        )}
      </Card>

      <Card title="Month by month">
        {!months ? (
          <Spinner />
        ) : months.length === 0 ? (
          <Empty>No history yet — make a deposit or a trade to start the clock.</Empty>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="table-base">
                <thead>
                  <tr>
                    <th>Month</th>
                    <th className="text-right">Beginning</th>
                    <th className="text-right">Deposits &amp; withdrawals</th>
                    <th className="text-right">Market gain/loss</th>
                    <th className="text-right">Income</th>
                    <th className="text-right">Your return</th>
                    <th className="text-right">Cumulative</th>
                    <th className="text-right">Ending</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((m) => {
                    const open = expanded === m.month;
                    const rows = events[m.month];
                    return (
                      <Fragment key={m.month}>
                        <tr
                          onClick={() => toggle(m.month)}
                          className="cursor-pointer transition hover:bg-slate-800/40"
                          title="Show what happened in this month"
                        >
                          <td className="whitespace-nowrap font-medium">
                            <span aria-hidden className="mr-1.5 inline-block w-2 text-slate-500">
                              {open ? "▾" : "▸"}
                            </span>
                            {monthLabel(m.month)}
                            {isCurrentMonth(m.month) && (
                              <span className="ml-2 rounded-full border border-slate-700 bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                                so far
                              </span>
                            )}
                            {m.backdated_fills > 0 && (
                              <span className="ml-2 text-amber-400"
                                    title={`${m.backdated_fills} past-dated fill(s) effective this month`}>
                                ◷
                              </span>
                            )}
                          </td>
                          <td className="text-right tabular-nums">{money(m.beginning_balance)}</td>
                          <td className="text-right tabular-nums text-slate-300">
                            {parseFloat(m.net_cash_flow) === 0 ? money(0) : signedMoney(m.net_cash_flow)}
                          </td>
                          <td className={`text-right tabular-nums ${toneOf(m.market_gain)}`}>
                            {signedMoney(m.market_gain)}
                          </td>
                          <td className="text-right tabular-nums text-slate-300">{money(m.income)}</td>
                          <td className={`text-right font-medium tabular-nums ${toneOf(m.personal_return)}`}>
                            {signedMoney(m.personal_return)}
                          </td>
                          <td className={`text-right tabular-nums ${toneOf(m.cumulative_return)}`}>
                            {signedMoney(m.cumulative_return)}
                          </td>
                          <td className="text-right font-medium tabular-nums">{money(m.ending_balance)}</td>
                        </tr>
                        {open && (
                          <tr className="bg-slate-900/40">
                            <td colSpan={8} className="px-4 py-3">
                              {!rows ? (
                                <Spinner />
                              ) : rows.length === 0 ? (
                                <p className="py-2 text-center text-sm text-slate-500">
                                  Nothing happened this month — the change above is entirely market movement.
                                </p>
                              ) : (
                                <ul className="divide-y divide-slate-800/60">
                                  {rows.map((e, i) => (
                                    <li key={i} className="flex items-center justify-between gap-3 py-1.5 text-sm">
                                      <div className="min-w-0">
                                        <span className="text-slate-500">{shortDate(e.date)}</span>{" "}
                                        <span className="text-slate-200">{e.description}</span>
                                        {e.backdated && (
                                          <span className="ml-2 text-xs text-amber-400"
                                                title="Entered after the date it filled on">
                                            past-dated
                                          </span>
                                        )}
                                        <span className="block text-xs text-slate-500">{e.account}</span>
                                      </div>
                                      <span className={`shrink-0 tabular-nums ${toneOf(e.amount)}`}>
                                        {signedMoney(e.amount)}
                                      </span>
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {months.length > limit && (
              <button className="btn-ghost mt-4 w-full"
                      onClick={() => setLimit((n) => n + PAGE_MONTHS)}>
                Show {Math.min(PAGE_MONTHS, months.length - limit)} more month
                {Math.min(PAGE_MONTHS, months.length - limit) === 1 ? "" : "s"}
              </button>
            )}

            <p className="mt-4 border-t border-slate-800 pt-3 text-xs text-slate-500">
              Every row balances: <span className="text-slate-400">
              ending = beginning + deposits &amp; withdrawals + market gain/loss + income</span>.
              &ldquo;Your return&rdquo; is what the portfolio earned with deposits and withdrawals
              taken out of the picture, and cumulative is the running total of that since you
              started — it does not change when you show fewer months. The personal rate of return
              above is dollar-weighted (IRR), so it accounts for how much was invested and for how
              long, and will differ from a fund&rsquo;s published return.
            </p>
          </>
        )}
      </Card>
    </div>
  );
}
