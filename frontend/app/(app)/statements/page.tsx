"use client";

import { useEffect, useState } from "react";
import { api, StatementT } from "@/lib/api";
import { shortDate } from "@/lib/format";
import { Card, Empty, Spinner } from "@/components/ui";

export default function StatementsPage() {
  const [statements, setStatements] = useState<StatementT[] | null>(null);
  const [year, setYear] = useState<number | null>(null);

  useEffect(() => {
    // the API lazily generates any missing periods before listing
    api<StatementT[]>("/statements").then(setStatements).catch(() => setStatements([]));
  }, []);

  const byYear = new Map<number, StatementT[]>();
  for (const s of statements ?? []) {
    const y = new Date(s.period_start + "T00:00:00").getFullYear();
    byYear.set(y, [...(byYear.get(y) ?? []), s]);
  }
  const years = Array.from(byYear.keys()).sort((a, b) => b - a);

  // Default to the current year once it has statements. Until the January
  // statement posts on February 1st there is nothing for it yet, so the most
  // recent year with statements (last year) stays selected.
  useEffect(() => {
    if (year !== null || years.length === 0) return;
    const now = new Date().getFullYear();
    setYear(years.includes(now) ? now : years[0]);
  }, [years, year]);

  function label(s: StatementT): string {
    if (s.kind === "YEAR_END") return `Year-End Statement ${new Date(s.period_start + "T00:00:00").getFullYear()}`;
    return new Date(s.period_start + "T00:00:00").toLocaleDateString("en-US", { month: "long", year: "numeric" });
  }

  const shown = year === null ? [] : (byYear.get(year) ?? []);

  return (
    <div className="max-w-3xl space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Statements</h1>
          <p className="mt-1 text-sm text-slate-400">
            Monthly statements post on the 1st for each completed month; year-end statements include
            the full tax summary. All PDFs are letter-size and archived permanently.
          </p>
        </div>
        {years.length > 0 && (
          <div>
            <label className="label" htmlFor="stmt-year">Tax year</label>
            <select id="stmt-year" className="input w-40" value={year ?? ""}
                    onChange={(e) => setYear(parseInt(e.target.value, 10))}>
              {years.map((y) => (
                <option key={y} value={y}>{y}</option>
              ))}
            </select>
          </div>
        )}
      </header>

      {!statements ? (
        <div className="space-y-2">
          <Spinner />
          <p className="text-center text-xs text-slate-500">
            Preparing statements — first load renders any missing months…
          </p>
        </div>
      ) : statements.length === 0 ? (
        <Card>
          <Empty>
            No statements yet — statements are produced for each completed calendar month after
            your first deposit or trade.
          </Empty>
        </Card>
      ) : (
        <Card title={year ? String(year) : undefined}>
          {shown.length === 0 ? (
            <Empty>No statements for {year}.</Empty>
          ) : (
            <ul className="divide-y divide-slate-800/60">
              {[...shown]
                .sort((a, b) => (a.kind === "YEAR_END" ? -1 : b.kind === "YEAR_END" ? 1 : b.period_start.localeCompare(a.period_start)))
                .map((s) => (
                  <li key={s.id} className="flex items-center justify-between py-2.5">
                    <div>
                      <div className={`text-sm font-medium ${s.kind === "YEAR_END" ? "text-emerald-400" : "text-slate-200"}`}>
                        {label(s)}
                      </div>
                      <div className="text-xs text-slate-500">
                        {shortDate(s.period_start)} – {shortDate(s.period_end)}
                      </div>
                    </div>
                    <a
                      className="btn-ghost !py-1.5 text-xs"
                      href={`/api/v1/statements/${s.id}.pdf`}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      View PDF ↗
                    </a>
                  </li>
                ))}
            </ul>
          )}
        </Card>
      )}
    </div>
  );
}
