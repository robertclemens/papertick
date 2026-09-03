"use client";

import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountReturnsT,
  AccountT,
  api,
  ApiError,
  RANGE_LABEL,
  RANGES,
  RangeT,
} from "@/lib/api";
import { money, pct, shortDate, signedMoney } from "@/lib/format";
import { useMarketRefresh } from "@/lib/market-refresh";
import { Card, Dialog, Empty, ErrorText, Spinner, MarketStatus } from "@/components/ui";
import ContributionBars from "@/components/contribution-bars";
import { useRangePref } from "@/lib/prefs";

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<AccountT[] | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [range, setRange] = useRangePref();
  const [returns, setReturns] = useState<AccountReturnsT | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");

  function load() {
    api<AccountT[]>("/accounts").then(setAccounts).catch(() => setAccounts([]));
  }
  useEffect(load, []);

  /** Balances and per-account returns are priced off the live market, so they
   *  re-fetch on the same cadence as the dashboard. Quietly: no spinner over
   *  numbers already on screen. */
  const { status: marketStatus, lastRefresh, refreshing, refreshNow } = useMarketRefresh(() => {
    api<AccountT[]>("/accounts").then(setAccounts).catch(() => {});
    if (range) {
      api<AccountReturnsT>(`/portfolio/returns?range=${range}`).then(setReturns).catch(() => {});
    }
  });

  // balance / investment returns / rate of return follow the timeframe picker
  useEffect(() => {
    if (!range) return;   // wait for the user's preferred window
    setReturns(null);
    api<AccountReturnsT>(`/portfolio/returns?range=${range}`).then(setReturns).catch(() => setReturns(null));
  }, [range]);

  // one account per type: only offer the types the user does not have yet
  const ownedTypes = new Set((accounts ?? []).map((a) => a.account_type));
  const openTypes = Object.keys(ACCOUNT_TYPE_LABEL).filter((t) => !ownedTypes.has(t as any));
  useEffect(() => {
    if (!openTypes.includes(type)) setType(openTypes[0] ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts]);

  async function create(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await api("/accounts", { method: "POST", body: { name, account_type: type } });
      setOpen(false);
      setName("");
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create account");
    } finally {
      setBusy(false);
    }
  }

  async function saveName(account: AccountT) {
    const next = draftName.trim();
    setEditing(null);
    if (!next || next === account.name) return;
    setAccounts((prev) => prev?.map((a) => (a.id === account.id ? { ...a, name: next } : a)) ?? prev);
    await api(`/accounts/${account.id}`, { method: "PATCH", body: { name: next } }).catch(() => load());
  }

  /** Reorder locally for instant feedback; the server call just persists it. */
  function move(from: number, to: number) {
    setAccounts((prev) => {
      if (!prev || to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      const [row] = next.splice(from, 1);
      next.splice(to, 0, row);
      return next;
    });
  }

  function persistOrder(rows: AccountT[] | null) {
    if (!rows) return;
    api("/accounts/order", { method: "PUT", body: { account_ids: rows.map((a) => a.id) } })
      .catch(() => load());
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
          <p className="mt-1 text-sm text-slate-400">
            Taxable and tax-advantaged buckets, with IRS contribution rules enforced.
            Drag a row to reorder; click a name to rename it.
          </p>
          <MarketStatus status={marketStatus} lastRefresh={lastRefresh}
                        refreshing={refreshing} onRefresh={refreshNow} />
        </div>
        <div className="flex items-center gap-2">
          <div className="flex flex-wrap gap-1" role="group" aria-label="Returns timeframe">
            {RANGES.map((r) => (
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
          <button onClick={() => { setOpen(true); setError(""); }}
                  disabled={openTypes.length === 0}
                  title={openTypes.length === 0 ? "You already have one of every account type" : undefined}
                  className="btn-primary">
            Open account
          </button>
        </div>
      </header>

      {returns && accounts && accounts.length > 0 && (
        <div className="card flex flex-wrap items-baseline gap-x-8 gap-y-2">
          <div>
            <div className="text-xs text-slate-400">Total balance</div>
            <div className="text-xl font-semibold tabular-nums text-slate-50">
              {money(returns.total_balance)}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-400">Investment returns</div>
            <div className={`text-xl font-semibold tabular-nums ${
              parseFloat(returns.total_investment_returns) >= 0
                ? "text-(--status-good)" : "text-(--status-critical)"
            }`}>{signedMoney(returns.total_investment_returns)}</div>
          </div>
          <div>
            <div className="text-xs text-slate-400">Rate of return</div>
            <div className={`text-xl font-semibold tabular-nums ${
              (returns.total_rate_of_return_pct ?? 0) >= 0
                ? "text-(--status-good)" : "text-(--status-critical)"
            }`}>{pct(returns.total_rate_of_return_pct)}</div>
          </div>
          <div className="ml-auto text-xs text-slate-500">
            {range && RANGE_LABEL[range]}
            {returns.period_start && ` · ${shortDate(returns.period_start)} – ${shortDate(returns.period_end)}`}
          </div>
        </div>
      )}

      {!accounts ? (
        <Spinner />
      ) : accounts.length === 0 ? (
        <Card><Empty>No accounts yet — open your first bucket to start depositing.</Empty></Card>
      ) : (
        <ul className="space-y-4">
          {accounts.map((a, i) => {
            const r = returns?.accounts.find((x) => x.account_id === a.id);
            return (
              <li
                key={a.id}
                onDragOver={(e) => {
                  e.preventDefault();
                  if (!dragId || dragId === a.id) return;
                  const from = accounts.findIndex((x) => x.id === dragId);
                  if (from !== -1 && from !== i) move(from, i);
                }}
                onDrop={(e) => { e.preventDefault(); setDragId(null); persistOrder(accounts); }}
                className={`card transition ${
                  dragId === a.id ? "border-emerald-600 opacity-60" : "hover:border-slate-600"
                }`}
              >
                <div className="flex items-start gap-3">
                  <button
                    aria-label={`Reorder ${a.name}. Use the arrow keys to move it.`}
                    draggable
                    onDragStart={() => setDragId(a.id)}
                    onDragEnd={() => { setDragId(null); persistOrder(accounts); }}
                    onKeyDown={(e) => {
                      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
                      e.preventDefault();
                      const to = i + (e.key === "ArrowUp" ? -1 : 1);
                      if (to < 0 || to >= accounts.length) return;
                      move(i, to);
                      const next = [...accounts];
                      const [row] = next.splice(i, 1);
                      next.splice(to, 0, row);
                      persistOrder(next);
                    }}
                    className="mt-1 cursor-grab rounded px-1 text-lg leading-none text-slate-600 hover:text-slate-300 active:cursor-grabbing"
                  >
                    ⠿
                  </button>

                  <div className="min-w-0 flex-1">
                    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto]">
                      <div className="min-w-0">
                        <div className="flex items-center gap-3">
                          <span className="text-xs font-medium uppercase tracking-wide text-emerald-400">
                            {ACCOUNT_TYPE_LABEL[a.account_type]}
                          </span>
                          <Link href={`/accounts/${a.id}`}
                                className="text-xs text-slate-500 hover:text-slate-300">
                            View →
                          </Link>
                        </div>
                        {editing === a.id ? (
                          <input
                            autoFocus
                            aria-label="Account name"
                            className="input mt-1 max-w-sm"
                            maxLength={100}
                            value={draftName}
                            onChange={(e) => setDraftName(e.target.value)}
                            onBlur={() => saveName(a)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") { e.preventDefault(); saveName(a); }
                              if (e.key === "Escape") setEditing(null);
                            }}
                          />
                        ) : (
                          <button
                            onClick={() => { setEditing(a.id); setDraftName(a.name); }}
                            title="Rename"
                            className="mt-1 flex items-center gap-2 text-lg font-semibold text-slate-100 hover:text-emerald-300"
                          >
                            {a.name}
                            <span aria-hidden className="text-xs text-slate-500">✎</span>
                          </button>
                        )}

                        <div className="mt-4 flex flex-wrap gap-x-10 gap-y-3">
                          <div>
                            <div className="text-xs text-slate-400">Balance</div>
                            <div className="text-2xl font-semibold tabular-nums text-slate-50">
                              {money(r ? r.balance : a.settlement_balance)}
                            </div>
                          </div>
                          <div>
                            <div className="text-xs text-slate-400">Investment returns</div>
                            <div className={`text-2xl font-semibold tabular-nums ${
                              parseFloat(r?.investment_returns ?? "0") >= 0
                                ? "text-(--status-good)" : "text-(--status-critical)"
                            }`}>{r ? signedMoney(r.investment_returns) : "—"}</div>
                          </div>
                          <div>
                            <div className="text-xs text-slate-400">
                              Rate of return{r?.rate_of_return_annualized && " (ann.)"}
                            </div>
                            <div className={`text-2xl font-semibold tabular-nums ${
                              (r?.rate_of_return_pct ?? 0) >= 0
                                ? "text-(--status-good)" : "text-(--status-critical)"
                            }`}>{r ? pct(r.rate_of_return_pct) : "—"}</div>
                          </div>
                          <div>
                            <div className="text-xs text-slate-400">Settlement fund</div>
                            <div className="text-2xl font-semibold tabular-nums text-slate-300">
                              {money(a.settlement_balance)}
                            </div>
                          </div>
                        </div>
                        <div className="mt-3 text-xs text-slate-500">
                          {range && `${RANGE_LABEL[range]} · `}opened {shortDate(a.created_at)}
                        </div>
                      </div>

                      {a.contribution_statuses.length > 0 && (
                        <div className="lg:w-72 lg:border-l lg:border-slate-800 lg:pl-6">
                          <ContributionBars statuses={a.contribution_statuses} />
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <Dialog open={open} title="Open a new account" onClose={() => setOpen(false)}>
        <form onSubmit={create} className="space-y-4">
          <div>
            <label className="label" htmlFor="acct-type">Account type</label>
            <select id="acct-type" className="input" value={type} onChange={(e) => setType(e.target.value)}>
              {openTypes.map((k) => (
                <option key={k} value={k}>{ACCOUNT_TYPE_LABEL[k]}</option>
              ))}
            </select>
            <p className="mt-1 text-xs text-slate-500">
              One account per type. Types you already hold are not listed.
            </p>
          </div>
          <div>
            <label className="label" htmlFor="acct-name">Nickname</label>
            <input id="acct-name" required maxLength={100} className="input" placeholder="e.g. Long-term growth"
                   value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <ErrorText>{error}</ErrorText>
          <button type="submit" disabled={busy || !type} className="btn-primary w-full">
            {busy ? "Creating…" : "Create account"}
          </button>
        </form>
      </Dialog>
    </div>
  );
}
