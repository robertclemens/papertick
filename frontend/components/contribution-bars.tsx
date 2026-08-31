"use client";

import { ContributionStatusT } from "@/lib/api";
import { money, shortDate } from "@/lib/format";

/** IRA contribution progress, one bar per open tax year.
 *
 *  Between January 1 and Tax Day the prior year is still fundable, so two bars
 *  can show at once — the prior-year one only while it has room left, since a
 *  full or lapsed year is not a bucket anyone can act on. */
export default function ContributionBars({ statuses, listClassName = "space-y-5" }: {
  statuses: ContributionStatusT[];
  /** container for the bars — stacked by default, side-by-side where there is room */
  listClassName?: string;
}) {
  if (statuses.length === 0) return null;
  return (
    <div>
      <div className={listClassName}>
      {statuses.map((cs) => {
        const used = Math.min(100, cs.used_pct);
        return (
          <div key={cs.tax_year}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
                {cs.tax_year} contributions
                {cs.is_prior_year && (
                  <span className="ml-1.5 rounded-full border border-amber-900 bg-amber-950/40 px-1.5 py-0.5 text-[10px] normal-case tracking-normal text-amber-300">
                    prior year
                  </span>
                )}
              </span>
              <span className="text-xs text-slate-500">of {money(cs.limit)}</span>
            </div>
            <div className="mt-1.5 h-2.5 overflow-hidden rounded-full bg-slate-800"
                 role="progressbar" aria-valuenow={Math.round(used)}
                 aria-valuemin={0} aria-valuemax={100}
                 aria-label={`${cs.tax_year} IRA contribution limit used`}>
              <div className={`h-full rounded-full transition-all ${
                cs.is_prior_year ? "bg-amber-500" : "bg-emerald-500"
              }`} style={{ width: `${used}%` }} />
            </div>
            <dl className="mt-1.5 space-y-0.5 text-xs">
              <div className="flex justify-between gap-3">
                <dt className="text-slate-400">Contributed</dt>
                <dd className="tabular-nums text-slate-200">
                  {money(cs.contributed)} · {cs.used_pct.toFixed(1)}%
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-slate-400">Remaining</dt>
                <dd className="tabular-nums text-slate-200">
                  {money(cs.remaining)} · {(100 - used).toFixed(1)}%
                </dd>
              </div>
              {parseFloat(cs.contributed_here) !== parseFloat(cs.contributed) && (
                <div className="flex justify-between gap-3">
                  <dt className="text-slate-400">From this account</dt>
                  <dd className="tabular-nums text-slate-300">{money(cs.contributed_here)}</dd>
                </div>
              )}
            </dl>
            {cs.is_prior_year && cs.designation_deadline && (
              <p className="mt-1 text-xs text-amber-300/80">
                Still open until {shortDate(cs.designation_deadline)} — after that this room
                is gone for good.
              </p>
            )}
          </div>
        );
      })}
      </div>
      <p className="mt-4 text-xs text-slate-500">
        Shared across all your IRAs
        {statuses[0].catchup_included && " · includes age-50+ catch-up"}.
      </p>
    </div>
  );
}
