"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  ApiError,
  COST_BASIS_LABEL,
  ExchangePreviewT,
  ExchangeResultT,
  PositionT,
} from "@/lib/api";
import { money, shares as fmtShares, shortDate } from "@/lib/format";
import { Badge, Card, ErrorText, InfoText, Spinner } from "@/components/ui";
import SymbolSearch from "@/components/symbol-search";

type AmountMode = "DOLLARS" | "SHARES" | "ALL";

/** Exchange = sell one holding and reinvest the proceeds in another, in one
 *  instruction. In a taxable account the sale is a realization event, so the
 *  ticket previews exactly what would become taxable before anything runs. */
export default function ExchangeForm({ accounts, onExecuted }: {
  accounts: AccountT[];
  onExecuted?: () => void;
}) {
  const [accountId, setAccountId] = useState("");
  const [positions, setPositions] = useState<PositionT[] | null>(null);
  const [fromTicker, setFromTicker] = useState("");
  const [toTicker, setToTicker] = useState("");
  const [amountMode, setAmountMode] = useState<AmountMode>("DOLLARS");
  const [quantity, setQuantity] = useState("");
  const [preview, setPreview] = useState<ExchangePreviewT | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ExchangeResultT | null>(null);

  const account = useMemo(() => accounts.find((a) => a.id === accountId), [accounts, accountId]);
  const taxable = account?.account_type === "TAXABLE";

  useEffect(() => {
    if (accounts.length && !accountId) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  useEffect(() => {
    setPositions(null);
    setFromTicker("");
    if (!accountId) return;
    api<PositionT[]>(`/portfolio/positions?account_id=${accountId}`)
      .then((rows) => {
        setPositions(rows);
        if (rows.length) setFromTicker(rows[0].ticker);
      })
      .catch(() => setPositions([]));
  }, [accountId]);

  const body = useCallback(() => ({
    account_id: accountId,
    from_ticker: fromTicker,
    to_ticker: toTicker,
    quantity_type: amountMode === "SHARES" ? "SHARES" : "DOLLARS",
    quantity: amountMode === "ALL" ? null : quantity,
    exchange_all: amountMode === "ALL",
  }), [accountId, fromTicker, toTicker, amountMode, quantity]);

  const ready = Boolean(
    accountId && fromTicker && toTicker && fromTicker !== toTicker &&
    (amountMode === "ALL" || parseFloat(quantity) > 0)
  );

  // live tax/consequence preview — read-only, nothing is written
  useEffect(() => {
    if (!ready) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      api<ExchangePreviewT>("/orders/exchange/preview", { method: "POST", body: body() })
        .then((p) => { if (!cancelled) { setPreview(p); setPreviewError(""); } })
        .catch((err) => {
          if (cancelled) return;
          setPreview(null);
          setPreviewError(err instanceof ApiError ? err.message : "Preview unavailable");
        });
    }, 350);
    return () => { cancelled = true; clearTimeout(t); };
  }, [ready, body]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setResult(null);
    setBusy(true);
    try {
      setResult(await api<ExchangeResultT>("/orders/exchange", { method: "POST", body: body() }));
      onExecuted?.();
      api<PositionT[]>(`/portfolio/positions?account_id=${accountId}`).then(setPositions).catch(() => {});
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Exchange failed");
    } finally {
      setBusy(false);
    }
  }

  const seg = (active: boolean) =>
    `flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition ${
      active ? "bg-slate-700 text-slate-50" : "text-slate-400 hover:text-slate-200"
    }`;

  const held = positions?.find((p) => p.ticker === fromTicker);

  return (
    <div className="grid items-start gap-4 lg:grid-cols-3">
      <Card title="Exchange" className="lg:col-span-2">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="label" htmlFor="x-account">Account</label>
            <select id="x-account" required className="input" value={accountId}
                    onChange={(e) => setAccountId(e.target.value)}>
              {accounts.length === 0 && <option value="">No accounts — open one first</option>}
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-slate-500">
              An exchange stays inside one account: it sells one holding and buys another
              with the proceeds.
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="x-from">Exchange from</label>
              {positions === null ? (
                <Spinner />
              ) : positions.length === 0 ? (
                <p className="text-sm text-slate-500">
                  No holdings in this account yet — buy something first.
                </p>
              ) : (
                <select id="x-from" className="input" value={fromTicker}
                        onChange={(e) => setFromTicker(e.target.value)}>
                  {positions.map((p) => (
                    <option key={p.ticker} value={p.ticker}>
                      {p.ticker} · {fmtShares(p.shares)} sh · {money(p.market_value)}
                    </option>
                  ))}
                </select>
              )}
              {held && (
                <p className="mt-1 text-xs text-slate-500">
                  {held.name} · holding {money(held.market_value)}, gains{" "}
                  <span className={parseFloat(held.unrealized_gains) >= 0
                    ? "text-(--status-good)" : "text-(--status-critical)"}>
                    {money(held.unrealized_gains)}
                  </span>
                </p>
              )}
            </div>
            <div>
              <label className="label" htmlFor="x-to">Exchange into</label>
              <SymbolSearch
                id="x-to"
                value={toTicker}
                onSelect={setToTicker}
                placeholder="VTSAX, VOO, “vanguard 500”…"
              />
              {toTicker && toTicker === fromTicker && (
                <p className="mt-1 text-xs text-(--status-critical)">
                  Pick a different fund to exchange into.
                </p>
              )}
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <span className="label">Amount</span>
              <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1"
                   role="group" aria-label="Exchange amount type">
                <button type="button" className={seg(amountMode === "DOLLARS")}
                        onClick={() => setAmountMode("DOLLARS")}>Dollars</button>
                <button type="button" className={seg(amountMode === "SHARES")}
                        onClick={() => setAmountMode("SHARES")}>Shares</button>
                <button type="button" className={seg(amountMode === "ALL")}
                        onClick={() => setAmountMode("ALL")}>All</button>
              </div>
            </div>
            <div>
              <label className="label" htmlFor="x-qty">
                {amountMode === "ALL"
                  ? "Whole position"
                  : amountMode === "DOLLARS" ? "Amount (USD)" : "Shares (up to 6 decimals)"}
              </label>
              <input id="x-qty" type="number" min="0.000001" step="0.000001" className="input"
                     required={amountMode !== "ALL"} disabled={amountMode === "ALL"}
                     value={amountMode === "ALL" ? "" : quantity}
                     placeholder={amountMode === "ALL" ? "Entire holding" : ""}
                     onChange={(e) => setQuantity(e.target.value)} />
            </div>
          </div>

          {taxable && preview && (
            <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
              <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
                Cost basis
              </span>
              <p className="mt-1 text-sm text-slate-100">
                {COST_BASIS_LABEL[preview.cost_basis_method]}
              </p>
              <p className="mt-0.5 text-xs text-slate-500">
                Your election for {preview.from_ticker}, set on the account —{" "}
                <Link href={`/accounts/${accountId}`} className="text-emerald-400 hover:text-emerald-300">
                  change it there
                </Link>. To hand-pick lots, use the Sell ticket.
              </p>
            </div>
          )}

          <ErrorText>{error}</ErrorText>
          <button type="submit" disabled={busy || !ready} className="btn-primary w-full">
            {busy
              ? "Exchanging…"
              : `Exchange ${fromTicker || "—"} into ${toTicker || "—"}`}
          </button>
        </form>
      </Card>

      <div className="space-y-4">
        <Card title={preview?.taxable ? "Taxable impact" : "What happens"}>
          {!ready ? (
            <p className="text-sm text-slate-500">
              Pick what to exchange out of and into, and this shows exactly what the sale
              realizes before you place it.
            </p>
          ) : previewError ? (
            <ErrorText>{previewError}</ErrorText>
          ) : !preview ? (
            <Spinner />
          ) : (
            <div className="space-y-3 text-sm">
              <div className="space-y-1">
                <Row label={`Selling ${preview.from_ticker}`} value={`${fmtShares(preview.shares)} sh @ ${money(preview.price)}`} />
                <Row label="Gross proceeds" value={money(preview.gross_proceeds)} />
                {parseFloat(preview.fees) > 0 && <Row label="Fees" value={`-${money(preview.fees)}`} />}
                <Row label="Reinvested" value={money(preview.net_proceeds)} />
                {preview.estimated_shares_bought && (
                  <Row
                    label={`Buys ${preview.to_ticker}`}
                    value={`≈ ${fmtShares(preview.estimated_shares_bought)} sh`}
                  />
                )}
              </div>

              {preview.taxable ? (
                <div className="space-y-1 border-t border-slate-800 pt-3">
                  <Row label="Cost basis" value={money(preview.cost_basis)} />
                  <Row
                    label="Short-term gains"
                    value={money(preview.short_term_gains)}
                    tone={parseFloat(preview.short_term_gains)}
                  />
                  <Row
                    label="Long-term gains"
                    value={money(preview.long_term_gains)}
                    tone={parseFloat(preview.long_term_gains)}
                  />
                  <div className="flex justify-between gap-3 border-t border-slate-800 pt-1.5 font-medium">
                    <span className="text-slate-300">Taxable gains</span>
                    <span className={`tabular-nums ${parseFloat(preview.total_gains) >= 0
                      ? "text-(--status-good)" : "text-(--status-critical)"}`}>
                      {money(preview.total_gains)}
                    </span>
                  </div>
                </div>
              ) : (
                <InfoText>
                  This account is tax-advantaged, so the exchange is not a taxable event —
                  no gain is reported and no 1099-B is issued. Nothing to elect, nothing to
                  report: the shares simply move from {preview.from_ticker} to{" "}
                  {preview.to_ticker}.
                </InfoText>
              )}

              {preview.taxable && preview.lots.length > 0 && (
                <details className="border-t border-slate-800 pt-3">
                  <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-200">
                    Lots being sold ({preview.lots.length}) · {preview.cost_basis_method}
                  </summary>
                  <table className="table-base mt-2">
                    <thead>
                      <tr><th>Acquired</th><th>Shares</th><th>Cost</th><th>Gain</th><th>Term</th></tr>
                    </thead>
                    <tbody>
                      {preview.lots.map((l, i) => (
                        <tr key={`${l.acquired_on}-${i}`}>
                          <td className="whitespace-nowrap">{shortDate(l.acquired_on)}</td>
                          <td>{fmtShares(l.shares)}</td>
                          <td>{money(l.cost_per_share)}</td>
                          <td className={parseFloat(l.gain) >= 0
                            ? "text-(--status-good)" : "text-(--status-critical)"}>
                            {money(l.gain)}
                          </td>
                          <td className={l.term === "LONG" ? "text-emerald-400" : "text-amber-400"}>
                            {l.term}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}

              <ul className="space-y-1 border-t border-slate-800 pt-3 text-xs text-slate-500">
                {preview.notes.map((n) => <li key={n}>• {n}</li>)}
              </ul>
            </div>
          )}
        </Card>

        {result && (
          <Card title="Exchange result">
            <div className="space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-slate-400">Sell {result.sell.order.ticker}</span>
                <Badge value={result.sell.order.status} />
              </div>
              {result.sell.transaction && (
                <Row
                  label="Filled"
                  value={`${fmtShares(result.sell.transaction.shares_filled)} @ ${money(result.sell.transaction.executed_price)}`}
                />
              )}
              {result.buy && (
                <>
                  <div className="flex items-center justify-between">
                    <span className="text-slate-400">Buy {result.buy.order.ticker}</span>
                    <Badge value={result.buy.order.status} />
                  </div>
                  {result.buy.transaction && (
                    <Row
                      label="Filled"
                      value={`${fmtShares(result.buy.transaction.shares_filled)} @ ${money(result.buy.transaction.executed_price)}`}
                    />
                  )}
                </>
              )}
              {result.taxable && result.realized_gains != null && (
                <div className="border-t border-slate-800 pt-2">
                  <Row label="Realized gains" value={money(result.realized_gains)}
                       tone={parseFloat(result.realized_gains)} />
                  <Row label="Short-term" value={money(result.short_term_gains)} />
                  <Row label="Long-term" value={money(result.long_term_gains)} />
                </div>
              )}
              {result.notes.map((n) => <InfoText key={n}>{n}</InfoText>)}
              {result.sell.order.status === "REJECTED" && (
                <ErrorText>{result.sell.order.reject_reason}</ErrorText>
              )}
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: number }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-slate-400">{label}</span>
      <span className={`tabular-nums ${
        tone === undefined ? "text-slate-200"
          : tone >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
      }`}>{value}</span>
    </div>
  );
}
