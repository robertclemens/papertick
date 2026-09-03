"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  ApiError,
  ChainT,
  OptionPositionViewT,
} from "@/lib/api";
import { money, pct, shortDate } from "@/lib/format";
import { useMarketRefresh } from "@/lib/market-refresh";
import { Badge, Card, Dialog, Empty, ErrorText, InfoText, MarketStatus, Spinner } from "@/components/ui";
import SymbolSearch from "@/components/symbol-search";

type Right = "CALL" | "PUT";
type Action = "BUY_TO_OPEN" | "SELL_TO_OPEN";

interface Ticket {
  right: Right;
  strike: string;
  quote: { bid: string; ask: string; mid: string; iv: number; delta: number };
}

export default function OptionsPage() {
  const [accounts, setAccounts] = useState<AccountT[]>([]);
  const [accountId, setAccountId] = useState("");
  const [underlying, setUnderlying] = useState("");
  const [loadedFor, setLoadedFor] = useState("");
  const [expirations, setExpirations] = useState<string[]>([]);
  const [expiry, setExpiry] = useState("");
  const [chain, setChain] = useState<ChainT | null>(null);
  const [loading, setLoading] = useState(false);
  const [chainError, setChainError] = useState("");
  const [showEducation, setShowEducation] = useState(false);

  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [action, setAction] = useState<Action>("BUY_TO_OPEN");
  const [contracts, setContracts] = useState("1");
  const [orderError, setOrderError] = useState("");
  const [busy, setBusy] = useState(false);
  const [resultMsg, setResultMsg] = useState("");

  const [positions, setPositions] = useState<OptionPositionViewT[] | null>(null);
  const [manage, setManage] = useState<{ view: OptionPositionViewT; mode: "close" | "exercise" } | null>(null);
  const [manageContracts, setManageContracts] = useState("1");
  const [manageError, setManageError] = useState("");

  function loadPositions() {
    api<OptionPositionViewT[]>("/options/positions").then(setPositions).catch(() => setPositions([]));
  }

  useEffect(() => {
    api<AccountT[]>("/accounts").then((rows) => {
      setAccounts(rows);
      if (rows.length) setAccountId((p) => p || rows[0].id);
    }).catch(() => {});
    loadPositions();
  }, []);

  async function loadChain(e?: FormEvent) {
    e?.preventDefault();
    const sym = underlying.trim().toUpperCase();
    if (!sym) return;
    setLoading(true);
    setChainError("");
    setChain(null);
    try {
      const exp = await api<{ expirations: string[] }>(`/options/expirations/${sym}`);
      setExpirations(exp.expirations);
      const chosen = expiry && exp.expirations.includes(expiry) ? expiry : exp.expirations[1] ?? exp.expirations[0];
      setExpiry(chosen);
      setChain(await api<ChainT>(`/options/chain/${sym}?expiry=${chosen}`));
      setLoadedFor(sym);
    } catch (err) {
      setChainError(err instanceof ApiError ? err.message : "Failed to load chain");
    } finally {
      setLoading(false);
    }
  }

  /** The whole chain is priced off the live underlying — every premium, greek
   *  and breakeven moves with spot — and open contracts are marked to market
   *  from those same prices, so both re-price together on the market cadence.
   *  Silent: no `setLoading`, so the table never blanks under the reader. */
  const { status: market, lastRefresh, refreshing, refreshNow } = useMarketRefresh(() => {
    if (loadedFor && expiry) {
      api<ChainT>(`/options/chain/${loadedFor}?expiry=${expiry}`).then(setChain).catch(() => {});
    }
    api<OptionPositionViewT[]>("/options/positions").then(setPositions).catch(() => {});
  });

  async function switchExpiry(exp: string) {
    setExpiry(exp);
    if (!loadedFor) return;
    setLoading(true);
    try {
      setChain(await api<ChainT>(`/options/chain/${loadedFor}?expiry=${exp}`));
    } catch (err) {
      setChainError(err instanceof ApiError ? err.message : "Failed to load chain");
    } finally {
      setLoading(false);
    }
  }

  const explanation = useMemo(() => {
    if (!ticket || !chain) return "";
    const n = Math.max(1, parseInt(contracts, 10) || 1);
    const sh = n * 100;
    const strike = parseFloat(ticket.strike);
    const ask = parseFloat(ticket.quote.ask);
    const bid = parseFloat(ticket.quote.bid);
    const debit = ask * sh;
    const credit = bid * sh;
    if (action === "BUY_TO_OPEN") {
      if (ticket.right === "CALL") {
        return `You are buying ${n} call contract${n > 1 ? "s" : ""}. Each contract controls 100 shares, so this gives you the RIGHT (never the obligation) to BUY ${sh} shares of ${chain.underlying} at $${strike.toFixed(2)} any time until ${shortDate(chain.expiry)}. You pay ≈ ${money(debit)} up front — that premium is the most you can lose. You profit if the stock rises; breakeven at expiry ≈ $${(strike + ask).toFixed(2)}.`;
      }
      return `You are buying ${n} put contract${n > 1 ? "s" : ""}. Each contract controls 100 shares, so this gives you the RIGHT (never the obligation) to SELL ${sh} shares of ${chain.underlying} at $${strike.toFixed(2)} any time until ${shortDate(chain.expiry)}. You pay ≈ ${money(debit)} up front — your maximum loss. You profit if the stock falls; breakeven at expiry ≈ $${(strike - ask).toFixed(2)}.`;
    }
    if (ticket.right === "CALL") {
      return `You are selling ${n} COVERED CALL${n > 1 ? "s" : ""}: you collect ≈ ${money(credit)} of premium now, and in exchange you take on the OBLIGATION to sell ${sh} shares of ${chain.underlying} you already own at $${strike.toFixed(2)} if assigned (typically when the stock closes above the strike at expiry, ${shortDate(chain.expiry)}). The premium is yours either way, but your upside above $${strike.toFixed(2)} is capped. Requires ${sh} uncommitted shares.`;
    }
    return `You are selling ${n} CASH-SECURED PUT${n > 1 ? "s" : ""}: you collect ≈ ${money(credit)} of premium now, and take on the OBLIGATION to buy ${sh} shares of ${chain.underlying} at $${strike.toFixed(2)} if assigned (typically when the stock closes below the strike at expiry, ${shortDate(chain.expiry)}). ${money(strike * sh)} of your settlement fund is reserved as collateral until then. If assigned, your effective cost is ≈ $${(strike - bid).toFixed(2)}/share.`;
  }, [ticket, chain, action, contracts]);

  async function submitOrder(e: FormEvent) {
    e.preventDefault();
    if (!ticket || !chain) return;
    setOrderError("");
    setBusy(true);
    try {
      const res = await api<{ explanation: string }>("/options/orders", {
        method: "POST",
        body: {
          account_id: accountId,
          underlying: chain.underlying,
          right: ticket.right,
          strike: ticket.strike,
          expiry: chain.expiry,
          action,
          contracts: parseInt(contracts, 10),
        },
      });
      setTicket(null);
      setResultMsg(res.explanation);
      loadPositions();
      api<AccountT[]>("/accounts").then(setAccounts).catch(() => {});
    } catch (err) {
      setOrderError(err instanceof ApiError ? err.message : "Order failed");
    } finally {
      setBusy(false);
    }
  }

  async function submitManage(e: FormEvent) {
    e.preventDefault();
    if (!manage) return;
    setManageError("");
    setBusy(true);
    try {
      const res = await api<{ explanation: string }>(
        `/options/positions/${manage.view.position.id}/${manage.mode}`,
        { method: "POST", body: { contracts: parseInt(manageContracts, 10) } }
      );
      setManage(null);
      setResultMsg(res.explanation);
      loadPositions();
    } catch (err) {
      setManageError(err instanceof ApiError ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  function posLabel(v: OptionPositionViewT): string {
    const p = v.position;
    return `${p.underlying} ${shortDate(p.expiry)} $${parseFloat(p.strike).toFixed(2)} ${p.right}`;
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Options</h1>
          <p className="mt-1 text-sm text-slate-400">
            Calls, puts, covered calls and cash-secured puts.{" "}
            <button className="text-emerald-400 hover:text-emerald-300"
                    onClick={() => setShowEducation((v) => !v)}>
              {showEducation ? "Hide the basics" : "New to options? Read the basics"}
            </button>
          </p>
          <MarketStatus status={market} lastRefresh={lastRefresh}
                        refreshing={refreshing} onRefresh={refreshNow} />
        </div>
      </header>

      {showEducation && (
        <Card title="Options in one minute">
          <div className="space-y-2 text-sm text-slate-300">
            <p>
              An option is a contract on <span className="font-medium text-slate-100">100 shares</span> of a stock
              or ETF. A <span className="font-medium text-emerald-400">CALL</span> is the right to <em>buy</em> those
              shares at a fixed <span className="font-medium">strike price</span> until the expiration date;
              a <span className="font-medium text-sky-400">PUT</span> is the right to <em>sell</em> them.
            </p>
            <p>
              <span className="font-medium text-slate-100">Buyers</span> pay a premium for that right and can never
              lose more than the premium. <span className="font-medium text-slate-100">Sellers</span> collect the
              premium but take on the matching obligation — so PaperTick only allows selling when it&apos;s safe:
              covered calls (you own the shares) and cash-secured puts (the purchase cash is reserved).
            </p>
            <p className="text-xs text-slate-500">
              Premiums shown here are model-derived (Black-Scholes on the live stock price), not exchange quotes.
              In-the-money options are exercised or assigned automatically at expiration.
            </p>
          </div>
        </Card>
      )}

      {resultMsg && <InfoText>{resultMsg}</InfoText>}

      <Card title="Option chain">
        <form onSubmit={loadChain} className="flex flex-wrap items-end gap-3">
          <div className="w-56">
            <label className="label" htmlFor="o-underlying">Underlying symbol or name</label>
            <SymbolSearch id="o-underlying" value={underlying} onSelect={setUnderlying}
                          placeholder="AAPL, “apple”…" />
          </div>
          {expirations.length > 0 && loadedFor && (
            <div>
              <label className="label" htmlFor="o-expiry">Expiration</label>
              <select id="o-expiry" className="input w-44" value={expiry}
                      onChange={(e) => switchExpiry(e.target.value)}>
                {expirations.map((x) => <option key={x} value={x}>{shortDate(x)}</option>)}
              </select>
            </div>
          )}
          <button type="submit" className="btn-primary" disabled={loading || !underlying.trim()}>
            {loading ? "Loading…" : "Load chain"}
          </button>
          {chain && (
            <span className="pb-2 text-sm text-slate-400">
              {chain.underlying} at <span className="tabular-nums text-slate-200">{money(chain.spot)}</span>
              {" · "}{chain.days_to_expiry} days to expiry
            </span>
          )}
        </form>
        <ErrorText>{chainError}</ErrorText>

        {chain && (
          <div className="mt-4 overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr>
                  <th colSpan={3} className="!text-emerald-400">Calls</th>
                  <th className="text-center">Strike</th>
                  <th colSpan={3} className="!text-sky-400">Puts</th>
                </tr>
                <tr>
                  <th>Bid / Ask</th><th>Mid</th><th>Δ</th>
                  <th></th>
                  <th>Bid / Ask</th><th>Mid</th><th>Δ</th>
                </tr>
              </thead>
              <tbody>
                {chain.rows.map((r) => (
                  <tr key={r.strike}>
                    <td className={r.call.itm ? "bg-emerald-950/30" : ""}>
                      <button className="text-slate-300 hover:text-emerald-300"
                              onClick={() => { setTicket({ right: "CALL", strike: r.strike, quote: r.call }); setAction("BUY_TO_OPEN"); setContracts("1"); setOrderError(""); setResultMsg(""); }}>
                        {parseFloat(r.call.bid).toFixed(2)} / {parseFloat(r.call.ask).toFixed(2)}
                      </button>
                    </td>
                    <td className={r.call.itm ? "bg-emerald-950/30" : ""}>{parseFloat(r.call.mid).toFixed(2)}</td>
                    <td className={`text-xs text-slate-500 ${r.call.itm ? "bg-emerald-950/30" : ""}`}>{r.call.delta.toFixed(2)}</td>
                    <td className="text-center font-medium text-slate-100">{parseFloat(r.strike).toFixed(2)}</td>
                    <td className={r.put.itm ? "bg-sky-950/30" : ""}>
                      <button className="text-slate-300 hover:text-sky-300"
                              onClick={() => { setTicket({ right: "PUT", strike: r.strike, quote: r.put }); setAction("BUY_TO_OPEN"); setContracts("1"); setOrderError(""); setResultMsg(""); }}>
                        {parseFloat(r.put.bid).toFixed(2)} / {parseFloat(r.put.ask).toFixed(2)}
                      </button>
                    </td>
                    <td className={r.put.itm ? "bg-sky-950/30" : ""}>{parseFloat(r.put.mid).toFixed(2)}</td>
                    <td className={`text-xs text-slate-500 ${r.put.itm ? "bg-sky-950/30" : ""}`}>{r.put.delta.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-slate-500">
              Shaded rows are in the money. Click a bid/ask to open a trade ticket. IV ≈{" "}
              {chain.rows.length ? pct(chain.rows[Math.floor(chain.rows.length / 2)].call.iv * 100, 1).replace("+", "") : "—"} at the money.
            </p>
          </div>
        )}
      </Card>

      <Card title="Open option positions">
        {!positions ? (
          <Spinner />
        ) : positions.length === 0 ? (
          <Empty>No option positions. Load a chain above to place your first contract.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-base">
              <thead>
                <tr>
                  <th>Contract</th><th>Side</th><th>Qty</th><th>Avg premium</th><th>Mark</th>
                  <th>Value</th><th>Gains</th><th>Expires</th><th className="text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((v) => {
                  const u = parseFloat(v.unrealized_gains);
                  return (
                    <tr key={v.position.id}>
                      <td>
                        <div className="font-medium text-slate-100">{posLabel(v)}</div>
                        <div className="text-xs text-slate-500">
                          underlying {money(v.underlying_price)}{v.itm ? " · ITM" : " · OTM"}
                          {parseFloat(v.position.collateral) > 0 && ` · ${money(v.position.collateral)} reserved`}
                        </div>
                      </td>
                      <td><Badge value={v.position.side === "LONG" ? "BUY" : "SELL"} /></td>
                      <td>{v.position.contracts}</td>
                      <td>{money(v.position.avg_premium)}</td>
                      <td>{money(v.mark)}</td>
                      <td>{money(v.market_value)}</td>
                      <td className={u >= 0 ? "text-(--status-good)" : "text-(--status-critical)"}>
                        {money(v.unrealized_gains)}
                      </td>
                      <td>{v.days_to_expiry}d</td>
                      <td className="text-right">
                        <div className="inline-flex gap-2">
                          <button className="text-xs text-slate-300 hover:text-slate-100"
                                  onClick={() => { setManage({ view: v, mode: "close" }); setManageContracts(String(v.position.contracts)); setManageError(""); setResultMsg(""); }}>
                            Close
                          </button>
                          {v.position.side === "LONG" && (
                            <button className="text-xs text-emerald-400 hover:text-emerald-300"
                                    onClick={() => { setManage({ view: v, mode: "exercise" }); setManageContracts(String(v.position.contracts)); setManageError(""); setResultMsg(""); }}>
                              Exercise
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Dialog open={ticket !== null} title={ticket ? `${chain?.underlying} $${parseFloat(ticket.strike).toFixed(2)} ${ticket.right}` : ""}
              onClose={() => setTicket(null)}>
        {ticket && chain && (
          <form onSubmit={submitOrder} className="space-y-4">
            <div>
              <label className="label" htmlFor="ot-account">Account</label>
              <select id="ot-account" className="input" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]} · {money(a.settlement_balance)}</option>
                ))}
              </select>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor="ot-action">Action</label>
                <select id="ot-action" className="input" value={action} onChange={(e) => setAction(e.target.value as Action)}>
                  <option value="BUY_TO_OPEN">Buy to open</option>
                  <option value="SELL_TO_OPEN">
                    {ticket.right === "CALL" ? "Sell to open (covered call)" : "Sell to open (cash-secured put)"}
                  </option>
                </select>
              </div>
              <div>
                <label className="label" htmlFor="ot-contracts">Contracts (×100 shares)</label>
                <input id="ot-contracts" type="number" min={1} max={1000} required className="input"
                       value={contracts} onChange={(e) => setContracts(e.target.value)} />
              </div>
            </div>
            <div className="rounded-lg border border-slate-700 bg-slate-950 p-3 text-sm leading-relaxed text-slate-300">
              {explanation}
            </div>
            <ErrorText>{orderError}</ErrorText>
            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? "Placing…" : action === "BUY_TO_OPEN"
                ? `Buy for ≈ ${money(parseFloat(ticket.quote.ask) * 100 * (parseInt(contracts, 10) || 1))}`
                : `Sell for ≈ ${money(parseFloat(ticket.quote.bid) * 100 * (parseInt(contracts, 10) || 1))} credit`}
            </button>
          </form>
        )}
      </Dialog>

      <Dialog open={manage !== null}
              title={manage ? `${manage.mode === "close" ? "Close" : "Exercise"} ${posLabel(manage.view)}` : ""}
              onClose={() => setManage(null)}>
        {manage && (
          <form onSubmit={submitManage} className="space-y-4">
            {manage.mode === "exercise" ? (
              <InfoText>
                Exercising {manage.view.position.right === "CALL"
                  ? `BUYS ${parseInt(manageContracts, 10) * 100 || 100} shares of ${manage.view.position.underlying} at the $${parseFloat(manage.view.position.strike).toFixed(2)} strike using your settlement fund; the premium you paid is added to the shares' cost basis.`
                  : `SELLS ${parseInt(manageContracts, 10) * 100 || 100} of your ${manage.view.position.underlying} shares at the $${parseFloat(manage.view.position.strike).toFixed(2)} strike; the premium you paid reduces the sale proceeds.`}
                {" "}Exercising forfeits any remaining time value — closing usually pays more unless the option is deep in the money.
              </InfoText>
            ) : (
              <p className="text-sm text-slate-400">
                Closing at the current {manage.view.position.side === "LONG" ? "bid" : "ask"} locks in your
                profit or loss and {manage.view.position.side === "SHORT" ? "ends the obligation (and releases any collateral)" : "returns the remaining time value"}.
              </p>
            )}
            <div>
              <label className="label" htmlFor="m-contracts">Contracts</label>
              <input id="m-contracts" type="number" min={1} max={manage.view.position.contracts} required
                     className="input" value={manageContracts}
                     onChange={(e) => setManageContracts(e.target.value)} />
            </div>
            <ErrorText>{manageError}</ErrorText>
            <button type="submit" disabled={busy} className="btn-primary w-full">
              {busy ? "Working…" : manage.mode === "close" ? "Close position" : "Exercise"}
            </button>
          </form>
        )}
      </Dialog>
    </div>
  );
}
