"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  ApiError,
  ConversionPreviewT,
  ConversionResultT,
  PositionT,
} from "@/lib/api";
import { money, shares as fmtShares } from "@/lib/format";
import { Card, ConfirmDialog, ErrorText, InfoText, Spinner } from "@/components/ui";

type AmountMode = "DOLLARS" | "SHARES";

/** Roth conversion: move pre-tax IRA money into a Roth and pay the tax now.
 *
 *  The only conversion that exists — Traditional or Rollover into Roth. It is
 *  irreversible (recharacterising a conversion ended with the TCJA in 2018) and
 *  its entire cost is a tax bill that arrives months later, so the split is
 *  shown before the button and confirmed after it. */
export default function ConversionForm({ accounts, onExecuted }: {
  accounts: AccountT[];
  onExecuted?: () => void;
}) {
  const sources = useMemo(
    () => accounts.filter((a) => a.account_type === "TRADITIONAL_IRA"
                             || a.account_type === "ROLLOVER_IRA"),
    [accounts],
  );
  const destinations = useMemo(
    () => accounts.filter((a) => a.account_type === "ROTH_IRA"),
    [accounts],
  );

  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [mode, setMode] = useState<AmountMode>("DOLLARS");
  const [amount, setAmount] = useState("");
  const [ticker, setTicker] = useState("");
  const [shareCount, setShareCount] = useState("");
  const [positions, setPositions] = useState<PositionT[] | null>(null);
  const [preview, setPreview] = useState<ConversionPreviewT | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ConversionResultT | null>(null);

  useEffect(() => {
    if (sources.length && !fromId) setFromId(sources[0].id);
  }, [sources, fromId]);
  useEffect(() => {
    if (destinations.length && !toId) setToId(destinations[0].id);
  }, [destinations, toId]);

  useEffect(() => {
    setPositions(null);
    setTicker("");
    if (!fromId) return;
    api<PositionT[]>(`/portfolio/positions?account_id=${fromId}`)
      .then((rows) => {
        setPositions(rows);
        if (rows.length) setTicker(rows[0].ticker);
      })
      .catch(() => setPositions([]));
  }, [fromId]);

  const body = useCallback(() => (
    mode === "SHARES"
      ? { to_account_id: toId, ticker, shares: shareCount }
      : { to_account_id: toId, amount }
  ), [mode, toId, ticker, shareCount, amount]);

  const ready = mode === "SHARES"
    ? Boolean(fromId && toId && ticker && parseFloat(shareCount) > 0)
    : Boolean(fromId && toId && parseFloat(amount) > 0);

  // the tax split is the whole decision, so it is quoted continuously rather
  // than behind a "calculate" button nobody would press twice
  useEffect(() => {
    if (!ready) { setPreview(null); setPreviewError(""); return; }
    let alive = true;
    const t = setTimeout(() => {
      api<ConversionPreviewT>(`/accounts/${fromId}/convert/preview`,
                              { method: "POST", body: body() })
        .then((p) => { if (alive) { setPreview(p); setPreviewError(""); } })
        .catch((err) => {
          if (!alive) return;
          setPreview(null);
          setPreviewError(err instanceof ApiError ? err.message : "Could not price this conversion");
        });
    }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [ready, fromId, body]);

  async function convert() {
    setBusy(true);
    setError("");
    try {
      const res = await api<ConversionResultT>(`/accounts/${fromId}/convert`,
                                               { method: "POST", body: body() });
      setResult(res);
      setConfirming(false);
      setAmount("");
      setShareCount("");
      setPreview(null);
      onExecuted?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Conversion failed");
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (sources.length === 0 || destinations.length === 0) {
    return (
      <Card title="Convert to Roth">
        <InfoText>
          A conversion moves money from a Traditional or Rollover IRA into a Roth IRA. You need
          both to do one{sources.length === 0
            ? " — open a Traditional or Rollover IRA first."
            : " — open a Roth IRA first."}
        </InfoText>
      </Card>
    );
  }

  return (
    <div className="grid items-start gap-4 lg:grid-cols-3">
      <Card title="Roth conversion" className="lg:col-span-2">
        <form onSubmit={(e: FormEvent) => { e.preventDefault(); setConfirming(true); }}
              className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="cv-from">Convert from</label>
              <select id="cv-from" className="input" value={fromId}
                      onChange={(e) => setFromId(e.target.value)}>
                {sources.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="cv-to">Convert into</label>
              <select id="cv-to" className="input" value={toId}
                      onChange={(e) => setToId(e.target.value)}>
                {destinations.map((a) => (
                  <option key={a.id} value={a.id}>{a.name} · Roth IRA</option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1 sm:max-w-xs"
               role="group" aria-label="What to convert">
            {(["DOLLARS", "SHARES"] as const).map((m) => (
              <button key={m} type="button"
                      className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition ${
                        mode === m ? "bg-slate-700 text-slate-100" : "text-slate-400 hover:text-slate-200"
                      }`}
                      onClick={() => setMode(m)}>
                {m === "DOLLARS" ? "Cash" : "In kind"}
              </button>
            ))}
          </div>

          {mode === "DOLLARS" ? (
            <div>
              <label className="label" htmlFor="cv-amount">Amount to convert</label>
              <input id="cv-amount" className="input" inputMode="decimal" placeholder="10000.00"
                     value={amount} onChange={(e) => setAmount(e.target.value)} />
            </div>
          ) : !positions ? (
            <Spinner />
          ) : positions.length === 0 ? (
            <InfoText>This account holds no positions to convert in kind.</InfoText>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor="cv-ticker">Holding</label>
                <select id="cv-ticker" className="input" value={ticker}
                        onChange={(e) => setTicker(e.target.value)}>
                  {positions.map((p) => (
                    <option key={p.ticker} value={p.ticker}>
                      {p.ticker} — {fmtShares(p.shares)} shares
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label" htmlFor="cv-shares">Shares</label>
                <div className="flex gap-2">
                  <input id="cv-shares" className="input" inputMode="decimal"
                         value={shareCount} onChange={(e) => setShareCount(e.target.value)} />
                  <button type="button" className="btn-ghost shrink-0"
                          onClick={() => {
                            const p = positions.find((x) => x.ticker === ticker);
                            if (p) setShareCount(p.shares);
                          }}>
                    All
                  </button>
                </div>
              </div>
            </div>
          )}

          <ErrorText>{previewError || error}</ErrorText>

          <button type="submit" disabled={!ready || !preview || busy} className="btn-primary w-full">
            Review conversion
          </button>
        </form>
      </Card>

      <Card title="Tax consequence">
        {!preview ? (
          <p className="py-6 text-center text-sm text-slate-500">
            Choose accounts and an amount to see what this would cost.
          </p>
        ) : (
          <div className="space-y-3">
            <dl className="space-y-2 text-sm">
              <div className="flex justify-between gap-3">
                <dt className="text-slate-400">Converting</dt>
                <dd className="font-medium tabular-nums text-slate-100">
                  {money(preview.gross_amount)}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-slate-400">Ordinary income</dt>
                <dd className="font-medium tabular-nums text-(--status-critical)">
                  {money(preview.taxable_amount)}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-slate-400">Tax-free (basis)</dt>
                <dd className="font-medium tabular-nums text-(--status-good)">
                  {money(preview.nontaxable_amount)}
                </dd>
              </div>
              <div className="flex justify-between gap-3 border-t border-slate-800 pt-2">
                <dt className="text-slate-400">Basis across all pre-tax IRAs</dt>
                <dd className="tabular-nums text-slate-300">
                  {preview.basis_fraction_pct.toFixed(2)}%
                </dd>
              </div>
            </dl>
            <ul className="space-y-2 border-t border-slate-800 pt-3 text-xs text-slate-400">
              {preview.notes.map((n, i) => <li key={i}>· {n}</li>)}
            </ul>
          </div>
        )}
      </Card>

      {result && (
        <Card title="Converted" className="lg:col-span-3">
          <p className="text-sm text-slate-300">
            Converted {money(result.conversion.gross_amount)} into{" "}
            <span className="font-medium text-slate-100">{result.to_account.name}</span>.{" "}
            <span className="text-(--status-critical)">
              {money(result.conversion.taxable_amount)}
            </span>{" "}
            is ordinary income for {new Date(result.conversion.conversion_date).getFullYear()};{" "}
            {money(result.conversion.nontaxable_amount)} came out of after-tax basis.
          </p>
          <p className="mt-2 text-xs text-slate-500">
            Its five-year clock starts January 1,{" "}
            {new Date(result.conversion.conversion_date).getFullYear()}. See the Taxes page for
            the year&rsquo;s full picture.
          </p>
        </Card>
      )}

      <ConfirmDialog
        open={confirming}
        title="Convert to Roth?"
        confirmLabel="Convert"
        busy={busy}
        onConfirm={convert}
        onClose={() => setConfirming(false)}
      >
        {preview && (
          <>
            <p>
              Converting <span className="font-medium text-slate-100">
                {money(preview.gross_amount)}</span>
              {preview.in_kind && <> ({fmtShares(preview.shares)} {preview.ticker})</>} adds{" "}
              <span className="font-medium text-(--status-critical)">
                {money(preview.taxable_amount)}
              </span>{" "}
              to your {preview.five_year_clock_year} ordinary income.
            </p>
            <p>
              This cannot be undone — recharacterising a conversion was eliminated for 2018 and
              later by the Tax Cuts and Jobs Act.
            </p>
          </>
        )}
      </ConfirmDialog>
    </div>
  );
}
