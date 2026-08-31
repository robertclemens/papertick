"use client";

import { useParams } from "next/navigation";
import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  ApiError,
  COST_BASIS_LABEL,
  CostBasisConfigT,
  CostBasisMethodT,
  AllowedYearsT,
  OrderT,
  PositionT,
} from "@/lib/api";
import { expenseRatioPct, money, pct, shares, shortDate, signedMoney } from "@/lib/format";
import { Badge, Card, Dialog, Empty, ErrorText, InfoText, Spinner } from "@/components/ui";
import ContributionBars from "@/components/contribution-bars";

interface ContributionT {
  id: string;
  tax_year: number | null;
  amount: string;
  kind: string;
  memo: string | null;
  timestamp: string;
}

export default function AccountDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [account, setAccount] = useState<AccountT | null>(null);
  const [positions, setPositions] = useState<PositionT[] | null>(null);
  const [contributions, setContributions] = useState<ContributionT[] | null>(null);
  const [years, setYears] = useState<AllowedYearsT | null>(null);
  const [dialog, setDialog] = useState<"deposit" | "withdraw" | null>(null);
  const [amount, setAmount] = useState("");
  const [taxYear, setTaxYear] = useState<string>("");
  const [kind, setKind] = useState("CONTRIBUTION");
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [cbConfig, setCbConfig] = useState<CostBasisConfigT | null>(null);
  const [openOrders, setOpenOrders] = useState<OrderT[]>([]);
  const [cbTicker, setCbTicker] = useState("");
  const [cbMethod, setCbMethod] = useState<CostBasisMethodT>("FIFO");
  const [cbError, setCbError] = useState("");

  const isIra = account && account.account_type !== "TAXABLE";
  // A rollover holds rollover money only: contributing to it commingles the
  // account and forfeits rolling it into a future employer plan.
  const isRollover = account?.account_type === "ROLLOVER_IRA";

  const load = useCallback(() => {
    api<AccountT>(`/accounts/${id}`).then(setAccount).catch(() => setAccount(null));
    api<PositionT[]>(`/portfolio/positions?account_id=${id}`).then(setPositions).catch(() => setPositions([]));
    api<ContributionT[]>(`/accounts/${id}/contributions`).then(setContributions).catch(() => setContributions([]));
    api<CostBasisConfigT>(`/accounts/${id}/cost-basis`).then(setCbConfig).catch(() => {});
    Promise.all([
      api<OrderT[]>(`/orders?status=SCHEDULED&account_id=${id}&limit=50`),
      api<OrderT[]>(`/orders?status=PENDING&account_id=${id}&limit=50`),
    ])
      .then(([s, p]) => setOpenOrders([...s, ...p]))
      .catch(() => setOpenOrders([]));
  }, [id]);

  async function setCostBasis(method: CostBasisMethodT, ticker?: string) {
    setCbError("");
    try {
      const res = await api<CostBasisConfigT>(`/accounts/${id}/cost-basis`, {
        method: "PUT",
        body: { method, ticker: ticker || null },
      });
      setCbConfig(res);
      setCbTicker("");
    } catch (err) {
      setCbError(err instanceof ApiError ? err.message : "Failed to update cost basis");
    }
  }

  async function clearOverride(ticker: string) {
    try {
      setCbConfig(await api<CostBasisConfigT>(`/accounts/${id}/cost-basis/${ticker}`, { method: "DELETE" }));
    } catch { /* keep current view */ }
  }

  useEffect(load, [load]);

  useEffect(() => {
    if (isRollover) setKind("ROLLOVER");
  }, [isRollover]);

  useEffect(() => {
    if (!isIra || isRollover) return;
    api<AllowedYearsT>("/irs/allowed-years")
      .then((r) => {
        setYears(r);
        // default into the bucket that lapses first — prior year while it is
        // still open with room, otherwise this year
        setTaxYear(String(r.default_tax_year));
      })
      .catch(() => setYears(null));
  }, [isIra]);

  async function submitCashFlow(e: FormEvent) {
    e.preventDefault();
    setError("");
    setWarnings([]);
    setBusy(true);
    try {
      const path = dialog === "deposit" ? "deposit" : "withdraw";
      const body: any = { amount };
      if (dialog === "deposit" && isIra) {
        body.kind = kind;
        if (kind === "CONTRIBUTION" && taxYear) body.tax_year = parseInt(taxYear, 10);
      }
      const res = await api<{ warnings: string[] }>(`/accounts/${id}/${path}`, { method: "POST", body });
      setWarnings(res.warnings ?? []);
      setAmount("");
      load();
      if (isIra) api<AllowedYearsT>("/irs/allowed-years").then(setYears).catch(() => {});
      if (!res.warnings?.length) setDialog(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  if (!account) return <Spinner />;

  return (
    <div className="space-y-6">
      <header className="flex items-end justify-between">
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-emerald-400">
            {ACCOUNT_TYPE_LABEL[account.account_type]}
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">{account.name}</h1>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={() => { setDialog("withdraw"); setError(""); setWarnings([]); }}>
            Withdraw
          </button>
          <button className="btn-primary" onClick={() => { setDialog("deposit"); setError(""); setWarnings([]); }}>
            Deposit
          </button>
        </div>
      </header>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Settlement fund">
          <div className="text-3xl font-semibold tabular-nums">{money(account.settlement_balance)}</div>
          <div className="mt-1 text-xs text-slate-500">
            {account.settlement_ticker} · {account.settlement_name}
          </div>
          <div className="mt-2 space-y-0.5 text-xs text-slate-400">
            <div>
              {money(account.buying_power ?? account.settlement_balance)} available to trade —
              every dollar in the settlement fund is available immediately, with no holds.
            </div>
            {account.settlement_yield && (
              <div>
                Earning{" "}
                <span className="tabular-nums text-slate-200">
                  {(parseFloat(account.settlement_yield) * 100).toFixed(2)}%
                </span>{" "}
                (7-day SEC yield), accrued daily and paid as a dividend at month end
                {parseFloat(account.settlement_accrued ?? "0") > 0 && (
                  <> · {money(account.settlement_accrued)} accrued so far this month</>
                )}
                .
              </div>
            )}
          </div>
          <label className="mt-3 flex items-start gap-2 border-t border-slate-800 pt-3 text-xs text-slate-400">
            <input
              type="checkbox"
              className="mt-0.5 accent-emerald-500"
              checked={account.allow_external_funding}
              onChange={async (e) => {
                const next = e.target.checked;
                setAccount({ ...account, allow_external_funding: next });
                await api(`/accounts/${account.id}`, {
                  method: "PATCH",
                  body: { allow_external_funding: next },
                }).catch(() => load());
                load();
              }}
            />
            <span>
              Fund trades from my external bank when the settlement fund is short
              {isRollover
                ? " — unavailable here: a transfer in would be a regular contribution"
                : isIra
                  ? " (transfers count as contributions and stop at your annual limit)"
                  : ""}
            </span>
          </label>
        </Card>
        {isIra && account.contribution_statuses.length > 0 && (
          <Card title="IRA contribution limits" className="lg:col-span-2">
            <ContributionBars
              statuses={account.contribution_statuses}
              listClassName={account.contribution_statuses.length > 1
                ? "grid gap-6 sm:grid-cols-2" : "space-y-5"}
            />
          </Card>
        )}
      </div>

      {openOrders.length > 0 && (
        <Card title={`Open orders (${openOrders.length})`}>
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr><th>Placed</th><th>Side</th><th>Ticker</th><th>Type</th><th>Amount</th><th>Status</th><th>Expected</th></tr>
              </thead>
              <tbody>
                {openOrders.map((o) => (
                  <tr key={o.id}>
                    <td className="whitespace-nowrap">{shortDate(o.created_at)}</td>
                    <td><Badge value={o.side} /></td>
                    <td className="font-medium">{o.ticker}</td>
                    <td className="text-xs text-slate-400">
                      {o.order_type}{o.limit_price && ` @ ${money(o.limit_price)}`}
                    </td>
                    <td>{o.quantity_type === "DOLLARS" ? money(o.quantity) : `${shares(o.quantity)} sh`}</td>
                    <td><Badge value={o.status} /></td>
                    <td className="text-xs text-slate-400">
                      {o.nav_date ? `NAV ${shortDate(o.nav_date)}`
                        : o.scheduled_for ? shortDate(o.scheduled_for)
                        : o.expires_at ? `expires ${shortDate(o.expires_at)}` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            These earmark settlement money (buys) or shares (sells) until they fill, are
            cancelled, or expire.
          </p>
        </Card>
      )}

      <Card title="Positions in this account">
        {!positions ? (
          <Spinner />
        ) : positions.length === 0 && parseFloat(account.settlement_balance) <= 0 ? (
          <Empty>No positions in this account.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr><th>Ticker</th><th>Shares</th><th>Avg cost</th><th>Price</th><th>Value</th><th>Gains</th></tr>
              </thead>
              <tbody>
                {parseFloat(account.settlement_balance) > 0 && (
                  <tr>
                    <td>
                      <div className="font-medium text-slate-100">{account.settlement_ticker}</div>
                      <div className="text-xs text-slate-500">{account.settlement_name}</div>
                    </td>
                    <td>{shares(account.settlement_balance)}</td>
                    <td>{money(1)}</td>
                    <td>{money(1)}</td>
                    <td>{money(account.settlement_balance)}</td>
                    <td className="text-slate-500">—</td>
                  </tr>
                )}
                {positions.map((p) => {
                  const u = parseFloat(p.unrealized_gains);
                  return (
                    <tr key={p.ticker}>
                      <td>
                        <div className="font-medium text-slate-100">
                          {p.prospectus_url ? (
                            <a href={p.prospectus_url} target="_blank" rel="noopener noreferrer"
                               className="hover:text-emerald-300" title="Prospectus filings (SEC EDGAR)">
                              {p.ticker} ↗
                            </a>
                          ) : p.ticker}
                        </div>
                        <div className="text-xs text-slate-500">
                          {p.name}
                          {expenseRatioPct(p.expense_ratio) && ` · ER ${expenseRatioPct(p.expense_ratio)}`}
                        </div>
                      </td>
                      <td>{shares(p.shares)}</td>
                      <td>{money(p.average_cost)}</td>
                      <td>{money(p.price)}</td>
                      <td>{money(p.market_value)}</td>
                      <td className={u >= 0 ? "text-(--status-good)" : "text-(--status-critical)"}>
                        {signedMoney(p.unrealized_gains)} <span className="text-xs">({pct(p.unrealized_gains_pct)})</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {!isIra && <Card title="Cost basis method">
        {!cbConfig ? (
          <Spinner />
        ) : (
          <div className="space-y-4">
            <div className="max-w-md">
              <label className="label" htmlFor="cb-default">Account default (applies to every sale)</label>
              <select id="cb-default" className="input" value={cbConfig.default_method}
                      onChange={(e) => setCostBasis(e.target.value as CostBasisMethodT)}>
                {(Object.keys(COST_BASIS_LABEL) as CostBasisMethodT[])
                  .filter((m) => m !== "AVERAGE")
                  .map((m) => <option key={m} value={m}>{COST_BASIS_LABEL[m]}</option>)}
              </select>
              <p className="mt-1 text-xs text-slate-500">
                SPEC_ID asks you to pick lots on each sale. Average cost can only be elected per
                mutual fund below (IRS rule). You can also override the method on any single trade.
              </p>
            </div>
            <div>
              <span className="label">Per-fund overrides</span>
              {cbConfig.overrides.length > 0 && (
                <ul className="mb-2 space-y-1">
                  {cbConfig.overrides.map((o) => (
                    <li key={o.ticker} className="flex items-center gap-3 text-sm">
                      <span className="w-16 font-medium text-slate-100">{o.ticker}</span>
                      <span className="text-slate-400">{COST_BASIS_LABEL[o.method]}</span>
                      {o.average_locked && (
                        <span className="rounded-full border border-amber-900 bg-amber-950/40 px-2 py-0.5 text-xs text-amber-300"
                              title="A sale has used average cost — the averaged basis of existing shares is permanent (IRS rule); only shares bought after a method change get actual cost.">
                          🔒 averaged basis locked
                        </span>
                      )}
                      <button className="text-xs text-red-400 hover:text-red-300"
                              onClick={() => clearOverride(o.ticker)}>Remove</button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="flex flex-wrap gap-2">
                <input aria-label="Override ticker" className="input w-28 uppercase" placeholder="Ticker"
                       value={cbTicker} onChange={(e) => setCbTicker(e.target.value.toUpperCase())} />
                <select aria-label="Override method" className="input w-64" value={cbMethod}
                        onChange={(e) => setCbMethod(e.target.value as CostBasisMethodT)}>
                  {(Object.keys(COST_BASIS_LABEL) as CostBasisMethodT[])
                    .map((m) => <option key={m} value={m}>{COST_BASIS_LABEL[m]}</option>)}
                </select>
                <button className="btn-ghost" disabled={!cbTicker}
                        onClick={() => setCostBasis(cbMethod, cbTicker)}>
                  Set override
                </button>
              </div>
            </div>
            <ErrorText>{cbError}</ErrorText>
            <ul className="space-y-1 border-t border-slate-800 pt-3 text-xs text-slate-500">
              {cbConfig.notes.map((n) => <li key={n}>• {n}</li>)}
            </ul>
          </div>
        )}
      </Card>}

      <Card title="Settlement fund activity">
        {!contributions ? (
          <Spinner />
        ) : contributions.length === 0 ? (
          <Empty>No deposits or withdrawals yet.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr><th>Date</th><th>Type</th><th>Tax year</th><th>Amount</th></tr>
              </thead>
              <tbody>
                {contributions.map((c) => (
                  <tr key={c.id}>
                    <td>{shortDate(c.timestamp)}</td>
                    <td>
                      <Badge value={c.kind} />
                      {c.memo && <div className="mt-0.5 text-xs text-slate-500">{c.memo}</div>}
                    </td>
                    <td>{c.tax_year ?? "—"}</td>
                    <td className={parseFloat(c.amount) >= 0 ? "text-(--status-good)" : "text-(--status-critical)"}>
                      {signedMoney(c.amount)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Dialog
        open={dialog !== null}
        title={dialog === "deposit" ? "Deposit into settlement fund" : "Withdraw from settlement fund"}
        onClose={() => setDialog(null)}
      >
        <form onSubmit={submitCashFlow} className="space-y-4">
          <div>
            <label className="label" htmlFor="cf-amount">Amount (USD)</label>
            <input id="cf-amount" type="number" min="0.01" step="0.01" required className="input"
                   value={amount} onChange={(e) => setAmount(e.target.value)} />
          </div>
          {dialog === "deposit" && isIra && (
            <>
              <div>
                <label className="label" htmlFor="cf-kind">Deposit type</label>
                <select id="cf-kind" className="input" value={kind} disabled={isRollover}
                        onChange={(e) => setKind(e.target.value)}>
                  {!isRollover && <option value="CONTRIBUTION">Annual contribution</option>}
                  <option value="ROLLOVER">Rollover (limit-exempt)</option>
                </select>
                {isRollover && (
                  <p className="mt-1 text-xs text-slate-500">
                    Rollover money only. A regular contribution would commingle this
                    account and forfeit the option to roll it into a future employer
                    plan — contribute to your Roth or Traditional IRA instead.
                  </p>
                )}
              </div>
              {kind === "CONTRIBUTION" && !isRollover && years && years.buckets.length > 0 && (
                <div>
                  <label className="label" htmlFor="cf-year">Tax year designation</label>
                  <select id="cf-year" className="input" value={taxYear}
                          onChange={(e) => setTaxYear(e.target.value)}>
                    {years.buckets.map((b) => (
                      <option key={b.tax_year} value={b.tax_year}>
                        {b.is_prior_year ? "Prior year" : "Current year"} ({b.tax_year}) —{" "}
                        {money(b.remaining)} left
                      </option>
                    ))}
                  </select>
                  {years.buckets.some((b) => b.is_prior_year) && (
                    <p className="mt-1 text-xs text-amber-300/80">
                      Your {years.default_tax_year} room is still open until{" "}
                      {shortDate(years.buckets[0].designation_deadline)} and is used first —
                      it disappears after that date.
                    </p>
                  )}
                </div>
              )}
            </>
          )}
          <ErrorText>{error}</ErrorText>
          {warnings.map((w) => <InfoText key={w}>{w}</InfoText>)}
          <button type="submit" disabled={busy} className="btn-primary w-full">
            {busy ? "Processing…" : dialog === "deposit" ? "Deposit" : "Withdraw"}
          </button>
        </form>
      </Dialog>
    </div>
  );
}
