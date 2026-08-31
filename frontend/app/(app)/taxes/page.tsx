"use client";

import { useEffect, useState } from "react";
import { ACCOUNT_TYPE_LABEL, AccountT, api, TaxReportT } from "@/lib/api";
import { money } from "@/lib/format";
import { Card, InfoText, Spinner } from "@/components/ui";

export default function TaxesPage() {
  const currentYear = new Date().getFullYear();
  const [year, setYear] = useState(currentYear);
  const [accountId, setAccountId] = useState("");
  const [accounts, setAccounts] = useState<AccountT[]>([]);
  const [report, setReport] = useState<TaxReportT | null>(null);
  const [years, setYears] = useState<number[]>([currentYear]);

  useEffect(() => {
    api<AccountT[]>("/accounts").then(setAccounts).catch(() => {});
    // only offer years that actually have activity
    api<{ years: number[] }>("/tax/years")
      .then((r) => {
        if (r.years.length) {
          setYears(r.years);
          if (!r.years.includes(year)) setYear(r.years[0]);
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setReport(null);
    const q = accountId ? `&account_id=${accountId}` : "";
    api<TaxReportT>(`/tax/report?year=${year}${q}`).then(setReport).catch(() => {});
  }, [year, accountId]);

  const csvHref = `/api/v1/tax/report.csv?year=${year}${accountId ? `&account_id=${accountId}` : ""}`;
  const totalGains = report
    ? parseFloat(report.short_term_gains) + parseFloat(report.long_term_gains) + parseFloat(report.unclassified_gains)
    : 0;

  const row = (label: string, value: string, hint?: string, colored = false) => {
    const n = parseFloat(value);
    return (
      <div className="flex items-baseline justify-between border-b border-slate-800/60 py-2.5">
        <div>
          <div className="text-sm text-slate-200">{label}</div>
          {hint && <div className="text-xs text-slate-500">{hint}</div>}
        </div>
        <div className={`text-sm font-medium tabular-nums ${
          !colored || n === 0 ? "text-slate-100" : n > 0 ? "text-(--status-good)" : "text-(--status-critical)"
        }`}>
          {money(value)}
        </div>
      </div>
    );
  };

  return (
    <div className="max-w-3xl space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Taxes</h1>
          <p className="mt-1 text-sm text-slate-400">
            What this year&apos;s activity would mean at tax time. FIFO cost basis.
          </p>
        </div>
        <div className="flex gap-2">
          <select aria-label="Tax year" className="input w-28" value={year}
                  onChange={(e) => setYear(parseInt(e.target.value, 10))}>
            {years.map((y) => <option key={y} value={y}>{y}</option>)}
          </select>
          <select aria-label="Account" className="input w-52" value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}>
            <option value="">All accounts</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>{a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]}</option>
            ))}
          </select>
          <a className="btn-ghost" href={csvHref} target="_blank" rel="noopener noreferrer">CSV</a>
        </div>
      </header>

      {!report ? (
        <Spinner />
      ) : (
        <>
          <Card title={`Taxable brokerage — ${report.year}`}>
            {row("Short-term capital gains", report.short_term_gains, "positions held ≤ 1 year · taxed as ordinary income", true)}
            {row("Long-term capital gains", report.long_term_gains, "positions held > 1 year · preferential rates", true)}
            {parseFloat(report.unclassified_gains) !== 0 &&
              row("Unclassified gains", report.unclassified_gains, "sales recorded before lot tracking", true)}
            {row("Dividend income", report.dividends, "qualified status depends on the fund — check its documentation")}
            {row("Fees paid", report.fees)}
            <div className="flex items-baseline justify-between pt-3">
              <div className="text-sm font-semibold text-slate-100">Total realized gains</div>
              <div className={`text-base font-semibold tabular-nums ${
                totalGains === 0 ? "text-slate-100" : totalGains > 0 ? "text-(--status-good)" : "text-(--status-critical)"
              }`}>
                {money(totalGains)}
              </div>
            </div>
          </Card>

          <Card title="Retirement accounts">
            {row("IRA contributions designated to this tax year", report.ira_contributions)}
            {row("Rollovers received", report.rollovers, "not subject to annual limits")}
            {row("Traditional / Rollover IRA withdrawals", report.traditional_withdrawals,
                 "would be ordinary income, plus a 10% penalty before age 59½")}
            {row("Roth IRA withdrawals", report.roth_withdrawals,
                 "qualified withdrawals are tax-free; early earnings may be taxed")}
          </Card>

          <div className="space-y-2">
            {report.notes.map((n) => <InfoText key={n}>{n}</InfoText>)}
          </div>
        </>
      )}
    </div>
  );
}
