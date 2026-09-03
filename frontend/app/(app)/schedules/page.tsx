"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  ApiError,
  AssetT,
  MaxFundingPlanT,
  ScheduleT,
} from "@/lib/api";
import { dateTime, money, shortDate } from "@/lib/format";
import { Badge, Card, Dialog, Empty, ErrorText, Spinner } from "@/components/ui";
import SymbolSearch from "@/components/symbol-search";

const CADENCES = [
  { value: "DAILY", label: "Every market day" },
  { value: "WEEKLY", label: "Weekly" },
  { value: "BIWEEKLY", label: "Every two weeks" },
  { value: "MONTHLY", label: "Monthly" },
  { value: "QUARTERLY", label: "Quarterly (every 3 months)" },
  { value: "ANNUALLY", label: "Annually" },
];
const DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const USES_MONTH_DAY = ["MONTHLY", "QUARTERLY", "ANNUALLY"];
const USES_WEEKDAY = ["WEEKLY", "BIWEEKLY"];

/** "17 Fridays" reads better than "17 runs" and makes the count checkable
 *  against a calendar — which is the number people compare against. */
function runNoun(count: number): string {
  return count === 1 ? "run" : "runs";
}

export default function SchedulesPage() {
  const [schedules, setSchedules] = useState<ScheduleT[] | null>(null);
  const [accounts, setAccounts] = useState<AccountT[]>([]);
  const [assets, setAssets] = useState<AssetT[]>([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ScheduleT | null>(null);
  const [accountId, setAccountId] = useState("");
  const [ticker, setTicker] = useState("");
  const [amount, setAmount] = useState("");
  const [cadence, setCadence] = useState("MONTHLY");
  const [dayOfWeek, setDayOfWeek] = useState("0");
  const [dayOfMonth, setDayOfMonth] = useState("1");
  const [monthOfYear, setMonthOfYear] = useState(String(new Date().getMonth() + 1));
  const [fundToLimit, setFundToLimit] = useState(false);
  const [plan, setPlan] = useState<MaxFundingPlanT | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const account = accounts.find((a) => a.id === accountId);
  // only Roth / Traditional have a limit to fill (a rollover takes no
  // contributions, a brokerage has no cap)
  const hasLimit = (account?.contribution_statuses.length ?? 0) > 0;

  function startEdit(s: ScheduleT) {
    setEditing(s);
    setAccountId(s.account_id);
    setTicker(s.ticker);
    setAmount(s.amount);
    setCadence(s.cadence);
    setDayOfWeek(String(s.day_of_week ?? 0));
    setDayOfMonth(String(s.day_of_month ?? 1));
    setMonthOfYear(String(s.month_of_year ?? new Date().getMonth() + 1));
    setFundToLimit(s.fund_to_limit);
    setError("");
    setOpen(true);
  }

  function startCreate() {
    setEditing(null);
    setTicker("");
    setAmount("");
    setCadence("MONTHLY");
    setDayOfWeek("0");
    setDayOfMonth("1");
    setFundToLimit(false);
    setPlan(null);
    setError("");
    setOpen(true);
  }

  function load() {
    api<ScheduleT[]>("/schedules").then(setSchedules).catch(() => setSchedules([]));
  }

  useEffect(() => {
    load();
    api<AccountT[]>("/accounts").then((rows) => {
      setAccounts(rows);
      if (rows.length) setAccountId((prev) => prev || rows[0].id);
    }).catch(() => {});
    api<AssetT[]>("/market/assets").then(setAssets).catch(() => {});
  }, []);

  // Recompute whenever anything that changes the run count changes, and put
  // the per-run figure straight into the amount field.
  useEffect(() => {
    if (!open || !fundToLimit || !accountId || !hasLimit) {
      setPlan(null);
      return;
    }
    let cancelled = false;
    api<MaxFundingPlanT>("/schedules/max-funding", {
      method: "POST",
      body: {
        account_id: accountId,
        cadence,
        day_of_week: USES_WEEKDAY.includes(cadence) ? parseInt(dayOfWeek, 10) : null,
        day_of_month: USES_MONTH_DAY.includes(cadence) ? parseInt(dayOfMonth, 10) : null,
        month_of_year:
          cadence === "QUARTERLY" || cadence === "ANNUALLY" ? parseInt(monthOfYear, 10) : null,
      },
    })
      .then((p) => {
        if (cancelled) return;
        setPlan(p);
        if (p.eligible) setAmount(p.per_run);
      })
      .catch(() => { if (!cancelled) setPlan(null); });
    return () => { cancelled = true; };
  }, [open, fundToLimit, accountId, hasLimit, cadence, dayOfWeek, dayOfMonth, monthOfYear]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const body: any = { ticker, amount, cadence, fund_to_limit: fundToLimit && hasLimit };
      body.day_of_week = USES_WEEKDAY.includes(cadence) ? parseInt(dayOfWeek, 10) : null;
      body.day_of_month = USES_MONTH_DAY.includes(cadence) ? parseInt(dayOfMonth, 10) : null;
      body.month_of_year =
        cadence === "QUARTERLY" || cadence === "ANNUALLY" ? parseInt(monthOfYear, 10) : null;
      if (editing) {
        await api(`/schedules/${editing.id}`, { method: "PATCH", body });
      } else {
        await api("/schedules", { method: "POST", body: { ...body, account_id: accountId } });
      }
      setOpen(false);
      setEditing(null);
      setAmount("");
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save schedule");
    } finally {
      setBusy(false);
    }
  }

  async function action(id: string, verb: "pause" | "resume" | "cancel") {
    try {
      if (verb === "cancel") await api(`/schedules/${id}`, { method: "DELETE" });
      else await api(`/schedules/${id}/${verb}`, { method: "POST" });
      load();
    } catch {
      load();
    }
  }

  function cadenceLabel(s: ScheduleT): string {
    const dom = s.day_of_month ?? 1;
    const moy = MONTHS[(s.month_of_year ?? 1) - 1];
    if (s.cadence === "MONTHLY") return `Monthly on day ${dom}`;
    if (s.cadence === "QUARTERLY") return `Quarterly from ${moy}, day ${dom}`;
    if (s.cadence === "ANNUALLY") return `Every ${moy} ${dom}`;
    if (s.cadence === "WEEKLY") return `Weekly on ${DOW[s.day_of_week ?? 0]}`;
    if (s.cadence === "BIWEEKLY") return `Biweekly on ${DOW[s.day_of_week ?? 0]}`;
    return "Every market day";
  }

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Auto-Invest</h1>
          <p className="mt-1 text-sm text-slate-400">
            Recurring investments executed automatically — e.g. “$500 of VOO on the 1st”.
          </p>
        </div>
        <button className="btn-primary" onClick={startCreate}>New schedule</button>
      </header>

      <Card>
        {!schedules ? (
          <Spinner />
        ) : schedules.length === 0 ? (
          <Empty>No recurring investments yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr>
                  <th>Investment</th><th>Cadence</th><th>Next run</th><th>Last run</th>
                  <th>Status</th><th>Failures</th><th className="text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {schedules.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-slate-100">
                          {money(s.amount)} of {s.ticker}
                        </span>
                        {s.fund_to_limit && (
                          <span className="rounded-full border border-emerald-900 bg-emerald-950/40 px-1.5 py-0.5 text-[10px] text-emerald-300"
                                title="Each run is capped at the IRA contribution room left when it fires, so the year lands exactly on the limit.">
                            to limit
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-slate-500">
                        {accounts.find((a) => a.id === s.account_id)?.name ?? "…"}
                      </div>
                    </td>
                    <td>{cadenceLabel(s)}</td>
                    <td>{s.status === "ACTIVE" ? dateTime(s.next_run_at) : "—"}</td>
                    <td>{dateTime(s.last_run_at)}</td>
                    <td><Badge value={s.status} /></td>
                    <td>{s.failure_count > 0 ? (
                      <span className="text-(--status-critical)">{s.failure_count}</span>
                    ) : "0"}</td>
                    <td className="text-right">
                      {s.status !== "CANCELLED" && (
                        <div className="inline-flex gap-2">
                          <button className="text-xs text-emerald-400 hover:text-emerald-300"
                                  onClick={() => startEdit(s)}>Edit</button>
                          {s.status === "ACTIVE" ? (
                            <button className="text-xs text-slate-300 hover:text-slate-100"
                                    onClick={() => action(s.id, "pause")}>Pause</button>
                          ) : (
                            <button className="text-xs text-emerald-400 hover:text-emerald-300"
                                    onClick={() => action(s.id, "resume")}>Resume</button>
                          )}
                          <button className="text-xs text-red-400 hover:text-red-300"
                                  onClick={() => action(s.id, "cancel")}>Cancel</button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Dialog
        open={open}
        title={editing ? "Edit recurring investment" : "New recurring investment"}
        onClose={() => { setOpen(false); setEditing(null); }}
      >
        <form onSubmit={save} className="space-y-4">
          {editing && (
            <p className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-400">
              Changes apply to future runs only — investments this rule already made are unchanged.
            </p>
          )}
          <div>
            <label className="label" htmlFor="s-account">Account</label>
            <select id="s-account" required disabled={!!editing} className="input" value={accountId}
                    onChange={(e) => setAccountId(e.target.value)}>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="s-ticker">Symbol or name</label>
              <SymbolSearch id="s-ticker" value={ticker} onSelect={setTicker}
                            placeholder="VOO, “vanguard”…" />
            </div>
            <div>
              <label className="label" htmlFor="s-amount">Amount (USD)</label>
              <input id="s-amount" type="number" min="1" step="0.01" required className="input"
                     value={amount} readOnly={fundToLimit && !!plan?.eligible}
                     onChange={(e) => setAmount(e.target.value)} />
            </div>
          </div>

          <div>
            <label className="label" htmlFor="s-cadence">Cadence</label>
            <select id="s-cadence" className="input" value={cadence} onChange={(e) => setCadence(e.target.value)}>
              {CADENCES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </div>
          {USES_WEEKDAY.includes(cadence) && (
            <div>
              <label className="label" htmlFor="s-dow">Day of week</label>
              <select id="s-dow" className="input" value={dayOfWeek} onChange={(e) => setDayOfWeek(e.target.value)}>
                {DOW.map((d, i) => <option key={d} value={i}>{d}</option>)}
              </select>
            </div>
          )}
          {USES_MONTH_DAY.includes(cadence) && (
            <div className="grid gap-3 sm:grid-cols-2">
              {(cadence === "QUARTERLY" || cadence === "ANNUALLY") && (
                <div>
                  <label className="label" htmlFor="s-moy">
                    {cadence === "QUARTERLY" ? "Starting month" : "Month"}
                  </label>
                  <select id="s-moy" className="input" value={monthOfYear}
                          onChange={(e) => setMonthOfYear(e.target.value)}>
                    {MONTHS.map((m, i) => <option key={m} value={i + 1}>{m}</option>)}
                  </select>
                </div>
              )}
              <div className={cadence === "MONTHLY" ? "col-span-2" : ""}>
                <label className="label" htmlFor="s-dom">Day of month (1–28)</label>
                <input id="s-dom" type="number" min="1" max="28" required className="input"
                       value={dayOfMonth} onChange={(e) => setDayOfMonth(e.target.value)} />
              </div>
            </div>
          )}
          {cadence === "QUARTERLY" && (
            <p className="text-xs text-slate-500">
              Runs in {MONTHS[(parseInt(monthOfYear, 10) - 1) % 12]} and every third month after
              ({[0, 3, 6, 9].map((o) => MONTHS[(parseInt(monthOfYear, 10) - 1 + o) % 12].slice(0, 3)).join(", ")}).
            </p>
          )}
          {hasLimit && (
            <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
              <label className="flex items-start gap-2 text-sm text-slate-200">
                <input type="checkbox" className="mt-0.5 accent-emerald-500"
                       checked={fundToLimit}
                       onChange={(e) => setFundToLimit(e.target.checked)} />
                <span>
                  Fund to my contribution limit
                  <span className="mt-0.5 block text-xs text-slate-500">
                    Splits the room left in this year&apos;s IRA limit evenly across
                    the runs this schedule has left, and trims the last one so the
                    year lands exactly on the limit.
                  </span>
                </span>
              </label>
              {fundToLimit && !plan && (
                <p className="mt-2 text-xs text-slate-500">Working out the schedule…</p>
              )}
              {fundToLimit && plan && (
                <div className="mt-3 border-t border-slate-800 pt-3">
                  {plan.eligible ? (
                    <dl className="space-y-1 text-xs">
                      <div className="flex justify-between gap-3">
                        <dt className="text-slate-400">{plan.tax_year} room left</dt>
                        <dd className="tabular-nums text-slate-100">{money(plan.remaining)}</dd>
                      </div>
                      <div className="flex justify-between gap-3">
                        <dt className="text-slate-400">Per run</dt>
                        <dd className="tabular-nums text-slate-100">
                          {money(plan.per_run)} &times; {plan.runs}{" "}
                          {USES_WEEKDAY.includes(cadence)
                            ? `${DOW[parseInt(dayOfWeek, 10)]}s`
                            : runNoun(plan.runs)}
                        </dd>
                      </div>
                      {plan.final_run !== plan.per_run && (
                        <div className="flex justify-between gap-3">
                          <dt className="text-slate-400">Final run (trimmed)</dt>
                          <dd className="tabular-nums text-slate-100">{money(plan.final_run)}</dd>
                        </div>
                      )}
                      <div className="flex justify-between gap-3 border-t border-slate-800 pt-1 font-medium">
                        <dt className="text-slate-300">Total by {plan.last_run ? shortDate(plan.last_run) : "year end"}</dt>
                        <dd className="tabular-nums text-emerald-400">{money(plan.total)}</dd>
                      </div>
                    </dl>
                  ) : null}
                  {plan.eligible && (
                    <p className="mt-2 text-xs text-slate-500">
                      Counted from {plan.first_run ? shortDate(plan.first_run) : "the next run"};
                      change the cadence or day above and this recalculates. A different
                      weekday can leave a different number of runs in the year.
                    </p>
                  )}
                  <ul className="mt-2 space-y-1 text-xs text-slate-500">
                    {plan.notes.map((n) => <li key={n}>• {n}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}
          <ErrorText>{error}</ErrorText>
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Saving…" : editing ? "Save changes" : "Start auto-invest"}
          </button>
        </form>
      </Dialog>
    </div>
  );
}
