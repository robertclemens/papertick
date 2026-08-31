"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  ACCOUNT_TYPE_LABEL,
  AccountT,
  api,
  ApiError,
  AssetT,
  COST_BASIS_LABEL,
  CostBasisConfigT,
  CostBasisMethodT,
  LotT,
  MarketStatusT,
  OrderT,
  PositionT,
  QuoteT,
  TransactionT,
} from "@/lib/api";
import { dateTime, money, pct, shares as fmtShares, shortDate } from "@/lib/format";
import { Badge, Card, ErrorText, InfoText, Spinner } from "@/components/ui";
import SymbolSearch from "@/components/symbol-search";
import ExchangeForm from "@/components/exchange-form";

type Mode = "now" | "backtest" | "schedule";

export default function TradePage() {
  const [accounts, setAccounts] = useState<AccountT[]>([]);
  const [assets, setAssets] = useState<AssetT[]>([]);
  const [query, setQuery] = useState("");
  const [accountId, setAccountId] = useState("");
  const [ticker, setTicker] = useState("");
  const [quote, setQuote] = useState<QuoteT | null>(null);
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  // Buy and Sell share one order ticket; Exchange is its own two-leg ticket.
  const [ticket, setTicket] = useState<"BUY" | "SELL" | "EXCHANGE">("BUY");
  const [qtyType, setQtyType] = useState<"DOLLARS" | "SHARES">("DOLLARS");
  const [quantity, setQuantity] = useState("");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [limitPrice, setLimitPrice] = useState("");
  const [tif, setTif] = useState("GTC_60");
  const [mode, setMode] = useState<Mode>("now");
  const [asOf, setAsOf] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ order: OrderT; transaction: TransactionT | null; funding?: string | null } | null>(null);
  const [market, setMarket] = useState<MarketStatusT | null>(null);
  const [cbConfig, setCbConfig] = useState<CostBasisConfigT | null>(null);
  const [pickLots, setPickLots] = useState(false);
  const [lots, setLots] = useState<LotT[]>([]);
  const [positions, setPositions] = useState<PositionT[] | null>(null);
  const [pickedLots, setPickedLots] = useState<Record<string, string>>({});

  useEffect(() => {
    const fetchStatus = () =>
      api<MarketStatusT>("/market/status")
        .then((status) => {
          setMarket(status);
          if (!status.allow_backdated_trades) setMode((m) => (m === "backtest" ? "now" : m));
        })
        .catch(() => {});
    fetchStatus();
    const t = setInterval(fetchStatus, 60000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    api<AccountT[]>("/accounts").then((rows) => {
      setAccounts(rows);
      if (rows.length && !accountId) setAccountId(rows[0].id);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      api<AssetT[]>(`/market/assets${query ? `?query=${encodeURIComponent(query)}` : ""}`)
        .then(setAssets)
        .catch(() => setAssets([]));
    }, 200);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    setQuote(null);
    if (!ticker) return;
    api<QuoteT>(`/market/quote/${ticker}`)
      .then((q) => {
        setQuote(q);
        // a valid unknown symbol was just auto-registered — refresh the list
        setAssets((prev) => {
          if (!prev.some((a) => a.ticker === q.ticker)) {
            api<AssetT[]>(`/market/assets?query=${encodeURIComponent(q.ticker)}`)
              .then((rows) => setAssets((cur) => {
                const seen = new Set(cur.map((a) => a.ticker));
                return [...cur, ...rows.filter((a) => !seen.has(a.ticker))];
              }))
              .catch(() => {});
          }
          return prev;
        });
      })
      .catch(() => setQuote(null));
  }, [ticker]);

  const loadPositions = useCallback(() => {
    if (!accountId) {
      setPositions([]);
      return;
    }
    api<PositionT[]>(`/portfolio/positions?account_id=${accountId}`)
      .then(setPositions)
      .catch(() => setPositions([]));
  }, [accountId]);

  useEffect(() => { setPositions(null); loadPositions(); }, [loadPositions]);

  // a sell can only name something the account actually holds
  useEffect(() => {
    if (side !== "SELL" || !positions?.length) return;
    if (!positions.some((p) => p.ticker === ticker)) {
      setTicker(positions[0].ticker);
      setQuery(positions[0].ticker);
    }
  }, [side, positions, ticker]);

  const account = useMemo(() => accounts.find((a) => a.id === accountId), [accounts, accountId]);
  const selectedAsset = useMemo(() => assets.find((a) => a.ticker === ticker), [assets, ticker]);
  const isFund = selectedAsset?.asset_class === "MUTUAL_FUND";
  const effectiveOrderType = isFund ? "MARKET" : orderType;
  const specTotal = useMemo(
    () => Object.values(pickedLots).reduce((s, v) => s + (parseFloat(v) || 0), 0),
    [pickedLots]
  );

  // the cost-basis election lives on the account (and per fund) — the ticket
  // only reports which one will apply to this sale
  useEffect(() => {
    setCbConfig(null);
    if (!accountId || account?.account_type !== "TAXABLE") return;
    api<CostBasisConfigT>(`/accounts/${accountId}/cost-basis`)
      .then(setCbConfig)
      .catch(() => setCbConfig(null));
  }, [accountId, account?.account_type]);

  const tickerOverride = cbConfig?.overrides.find((o) => o.ticker === ticker);
  const effectiveMethod: CostBasisMethodT | null = cbConfig
    ? (tickerOverride?.method ?? cbConfig.default_method)
    : null;

  useEffect(() => {
    setPickedLots({});
    if (side === "SELL" && pickLots && accountId && ticker) {
      api<LotT[]>(`/portfolio/lots?account_id=${accountId}&ticker=${ticker}`)
        .then(setLots)
        .catch(() => setLots([]));
    } else {
      setLots([]);
    }
  }, [side, pickLots, accountId, ticker]);
  const estShares = useMemo(() => {
    if (!quote || !quantity) return null;
    const q = parseFloat(quantity);
    const p = parseFloat(quote.price);
    if (!isFinite(q) || !isFinite(p) || p <= 0) return null;
    return qtyType === "DOLLARS" ? q / p : q;
  }, [quote, quantity, qtyType]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setResult(null);
    setBusy(true);
    try {
      const body: any = {
        account_id: accountId,
        ticker,
        side,
        order_type: effectiveOrderType,
        quantity_type: qtyType,
        quantity,
      };
      // no method is sent: the server resolves the account/fund election
      if (side === "SELL" && pickLots) {
        body.quantity_type = "SHARES";
        body.quantity = String(specTotal);
        body.spec_lots = Object.entries(pickedLots)
          .filter(([, v]) => parseFloat(v) > 0)
          .map(([lot_id, shares]) => ({ lot_id, shares }));
      }
      if (effectiveOrderType === "LIMIT") {
        body.limit_price = limitPrice;
        body.time_in_force = tif;
      }
      if (mode === "backtest" && asOf) body.as_of = asOf;
      if (mode === "schedule" && scheduledFor) {
        body.scheduled_for = new Date(scheduledFor).toISOString();
      }
      const res = await api<{ order: OrderT; transaction: TransactionT | null; funding?: string | null }>("/orders", {
        method: "POST",
        body,
      });
      setResult(res);
      api<AccountT[]>("/accounts").then(setAccounts).catch(() => {});
      loadPositions();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Order failed");
    } finally {
      setBusy(false);
    }
  }

  const held = positions?.find((p) => p.ticker === ticker);

  const seg = (active: boolean) =>
    `flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition ${
      active ? "bg-slate-700 text-slate-50" : "text-slate-400 hover:text-slate-200"
    }`;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Trade</h1>
          <p className="mt-1 text-sm text-slate-400">
            Buy, sell, or exchange one holding for another — at live prices, on a past
            date, or scheduled for later.
          </p>
        </div>
        {market && market.enforce_market_hours && (
          <div className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm ${
            market.is_open
              ? "border-emerald-900 bg-emerald-950/40 text-emerald-300"
              : "border-amber-900 bg-amber-950/30 text-amber-200"
          }`}>
            <span aria-hidden className={`h-2 w-2 rounded-full ${market.is_open ? "bg-emerald-400" : "bg-amber-400"}`} />
            {market.is_open
              ? `Market open · closes ${dateTime(market.next_close)}`
              : `Market closed · orders queue for ${dateTime(market.next_open)}`}
          </div>
        )}
      </header>

      <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1 sm:max-w-md"
           role="group" aria-label="Transaction type">
        {(["BUY", "SELL", "EXCHANGE"] as const).map((t) => (
          <button
            key={t}
            type="button"
            className={seg(ticket === t)}
            onClick={() => {
              setTicket(t);
              if (t !== "EXCHANGE") setSide(t);
              setResult(null);
              setError("");
            }}
          >
            {t === "BUY" ? "Buy" : t === "SELL" ? "Sell" : "Exchange"}
          </button>
        ))}
      </div>

      {ticket === "EXCHANGE" && (
        <ExchangeForm
          accounts={accounts}
          onExecuted={() => { api<AccountT[]>("/accounts").then(setAccounts).catch(() => {}); }}
        />
      )}

      <div className={ticket === "EXCHANGE"
        ? "hidden"
        : "grid items-start gap-4 lg:grid-cols-3"}>
        <Card title={`${side === "BUY" ? "Buy" : "Sell"} order ticket`} className="lg:col-span-2">
          <form onSubmit={submit} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor="t-account">Account</label>
                <select id="t-account" required className="input" value={accountId}
                        onChange={(e) => setAccountId(e.target.value)}>
                  {accounts.length === 0 && <option value="">No accounts — open one first</option>}
                  {accounts.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} · {ACCOUNT_TYPE_LABEL[a.account_type]}
                      {/* a sale does not spend buying power, so it is noise here */}
                      {side === "BUY" && ` · ${money(a.buying_power ?? a.settlement_balance)} available`}
                    </option>
                  ))}
                </select>
                {account && side === "BUY" && (
                  <p className="mt-1 text-xs text-slate-500">
                    Available to trade: <span className="tabular-nums text-slate-300">{money(account.buying_power ?? account.settlement_balance)}</span>
                    {account.buying_power !== null &&
                      parseFloat(account.buying_power) < parseFloat(account.settlement_balance) &&
                      ` of ${money(account.settlement_balance)} in the settlement fund (rest committed to open orders or collateral)`}
                    {account.allow_external_funding && " · shortfalls draw from your external bank"}
                  </p>
                )}
              </div>
              <div>
                <label className="label" htmlFor="t-ticker">
                  {side === "SELL" ? "Holding to sell" : "Symbol or company name"}
                </label>
                {side === "SELL" ? (
                  positions === null ? (
                    <Spinner />
                  ) : positions.length === 0 ? (
                    <p className="text-sm text-slate-500">
                      No holdings in this account — nothing to sell yet.
                    </p>
                  ) : (
                    <select id="t-ticker" className="input" value={ticker}
                            onChange={(e) => { setTicker(e.target.value); setQuery(e.target.value); }}>
                      {positions.map((p) => (
                        <option key={p.ticker} value={p.ticker}>
                          {p.ticker} · {fmtShares(p.shares)} sh · {money(p.market_value)}
                        </option>
                      ))}
                    </select>
                  )
                ) : (
                  <SymbolSearch
                    id="t-ticker"
                    value={ticker}
                    onSelect={(t) => { setTicker(t); setQuery(t); }}
                    placeholder="VOO, AAPL, “apple”, “vanguard 500”…"
                  />
                )}
                {side === "SELL" && held && (
                  <p className="mt-1 text-xs text-slate-500">
                    {held.name} · gains{" "}
                    <span className={parseFloat(held.unrealized_gains) >= 0
                      ? "text-(--status-good)" : "text-(--status-critical)"}>
                      {money(held.unrealized_gains)}
                    </span>
                  </p>
                )}
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <span className="label">Amount in</span>
                <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1" role="group" aria-label="Quantity type">
                  <button type="button" className={seg(qtyType === "DOLLARS")} onClick={() => setQtyType("DOLLARS")}>Dollars</button>
                  <button type="button" className={seg(qtyType === "SHARES")} onClick={() => setQtyType("SHARES")}>Shares</button>
                </div>
              </div>
              <div>
                <label className="label" htmlFor="t-qty">
                  {qtyType === "DOLLARS" ? "Amount (USD)" : "Shares (up to 6 decimals)"}
                </label>
                <input id="t-qty" type="number" min="0.000001" step="0.000001" required className="input"
                       value={quantity} onChange={(e) => setQuantity(e.target.value)} />
                {estShares !== null && qtyType === "DOLLARS" && (
                  <p className="mt-1 text-xs text-slate-500">≈ {fmtShares(estShares)} shares at the current price</p>
                )}
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor="t-otype">Order type</label>
                <select id="t-otype" className="input" value={isFund ? "MARKET" : orderType}
                        disabled={isFund}
                        onChange={(e) => { setOrderType(e.target.value as any); if (e.target.value === "LIMIT") setMode("now"); }}>
                  <option value="MARKET">{isFund ? "Market (NAV)" : "Market"}</option>
                  {!isFund && <option value="LIMIT">Limit</option>}
                </select>
                {effectiveOrderType === "LIMIT" && (
                  <>
                    <input aria-label="Limit price" type="number" min="0.000001" step="0.000001" required
                           placeholder="Limit price" className="input mt-2"
                           value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} />
                    <label className="label mt-2" htmlFor="t-tif">Time in force</label>
                    <select id="t-tif" className="input" value={tif} onChange={(e) => setTif(e.target.value)}>
                      <option value="DAY">Day — expires at today&apos;s close</option>
                      <option value="GTC_30">Good till canceled — 30 days</option>
                      <option value="GTC_60">Good till canceled — 60 days</option>
                      <option value="GTC_90">Good till canceled — 90 days</option>
                      <option value="GTC_180">Good till canceled — 180 days</option>
                      <option value="GTC">Good till canceled — 1 year (max)</option>
                    </select>
                  </>
                )}
              </div>
            </div>

            {effectiveOrderType === "MARKET" && (
              <div>
                <span className="label">Execution</span>
                <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1" role="group" aria-label="Execution mode">
                  <button type="button" className={seg(mode === "now")} onClick={() => setMode("now")}>Now</button>
                  {market?.allow_backdated_trades && (
                    <button type="button" className={seg(mode === "backtest")} onClick={() => setMode("backtest")}>Past date</button>
                  )}
                  <button type="button" className={seg(mode === "schedule")} onClick={() => setMode("schedule")}>Schedule</button>
                </div>
                {mode === "backtest" && market?.allow_backdated_trades && (
                  <div className="mt-2">
                    <input aria-label="Backtest date" type="date" required className="input"
                           min="2010-01-04" max={new Date(Date.now() - 86400000).toISOString().slice(0, 10)}
                           value={asOf} onChange={(e) => setAsOf(e.target.value)} />
                    <p className="mt-1 text-xs text-slate-500">
                      Pretend you invested on this date — filled at that day&apos;s closing price;
                      gains and losses since then flow into today&apos;s balance.
                    </p>
                  </div>
                )}
                {mode === "schedule" && (
                  <div className="mt-2">
                    <input aria-label="Scheduled time" type="datetime-local" required className="input"
                           value={scheduledFor} onChange={(e) => setScheduledFor(e.target.value)} />
                    <p className="mt-1 text-xs text-slate-500">Executed by the worker at or shortly after this time.</p>
                  </div>
                )}
              </div>
            )}

            {side === "SELL" && account?.account_type !== "TAXABLE" && (
              <p className="text-xs text-slate-500">
                Cost-basis elections don&apos;t apply in IRAs (no capital-gains treatment) — sales use FIFO.
              </p>
            )}
            {side === "SELL" && account?.account_type === "TAXABLE" && (
              <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-xs font-medium uppercase tracking-wide text-slate-400">
                    Cost basis
                  </span>
                  <Link href={`/accounts/${accountId}`}
                        className="text-xs text-emerald-400 hover:text-emerald-300">
                    Change this election →
                  </Link>
                </div>
                <p className="mt-1 text-sm text-slate-100">
                  {effectiveMethod ? COST_BASIS_LABEL[effectiveMethod] : "Loading…"}
                </p>
                <p className="mt-0.5 text-xs text-slate-500">
                  {tickerOverride
                    ? `Your ${ticker} election`
                    : "Your account default"} — set once on the account and applied to
                  every sale, the way a brokerage does it.
                </p>
                <label className="mt-3 flex items-center gap-2 text-xs text-slate-400">
                  <input type="checkbox" className="accent-emerald-500"
                         checked={pickLots} onChange={(e) => setPickLots(e.target.checked)} />
                  Hand-pick the lots for this one sale instead
                </label>
                {pickLots && (
                  <div className="mt-2 rounded-lg border border-slate-700 bg-slate-950 p-3">
                    {lots.length === 0 ? (
                      <p className="text-xs text-slate-500">No open lots for {ticker || "this symbol"} in this account.</p>
                    ) : (
                      <>
                        <div className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">
                          Choose lots to sell (shares per lot)
                        </div>
                        <ul className="space-y-1.5">
                          {lots.map((l) => (
                            <li key={l.id} className="flex items-center gap-3 text-xs">
                              <span className="w-20 text-slate-300">{shortDate(l.acquired_on)}</span>
                              <span className="w-24 tabular-nums text-slate-400">{fmtShares(l.shares_open)} sh</span>
                              <span className="w-20 tabular-nums text-slate-400">@{money(l.cost_per_share)}</span>
                              <span className={`w-12 ${l.term === "LONG" ? "text-emerald-400" : "text-amber-400"}`}>{l.term}</span>
                              <span className={`w-20 tabular-nums ${parseFloat(l.unrealized_gains ?? "0") >= 0 ? "text-(--status-good)" : "text-(--status-critical)"}`}>
                                {l.unrealized_gains ? money(l.unrealized_gains) : "—"}
                              </span>
                              <input aria-label={`Shares from lot acquired ${l.acquired_on}`} type="number"
                                     min="0" max={l.shares_open} step="0.000001"
                                     className="input !w-24 !py-1 text-xs"
                                     value={pickedLots[l.id] ?? ""}
                                     onChange={(e) => setPickedLots((p) => ({ ...p, [l.id]: e.target.value }))} />
                            </li>
                          ))}
                        </ul>
                        <p className="mt-2 text-xs text-slate-400">
                          Selected: <span className="tabular-nums text-slate-200">{fmtShares(specTotal)}</span> shares
                          (this replaces the quantity field above)
                        </p>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}

            {effectiveOrderType === "LIMIT" && side === "BUY" && (
              <InfoText>
A resting buy order earmarks its money in the settlement fund until it fills, is
                cancelled, or expires — it won&apos;t be available for other trades or
                withdrawals in the meantime.
              </InfoText>
            )}
            {effectiveOrderType === "LIMIT" && side === "SELL" && (
              <InfoText>
                A resting sell order reserves the shares it would sell, so they can&apos;t back
                another order until it fills, is cancelled, or expires.
              </InfoText>
            )}
            {isFund && mode === "now" && (
              <InfoText>
                Mutual funds trade once daily: this order fills at the closing NAV
                (orders in before 4:00 PM ET get today&apos;s NAV, later ones the next
                trading day&apos;s).
              </InfoText>
            )}
            <ErrorText>{error}</ErrorText>
            <button type="submit" disabled={busy || !accountId || !ticker} className="btn-primary w-full">
              {busy ? "Placing order…" : `${side === "BUY" ? "Buy" : "Sell"} ${ticker || "—"}`}
            </button>
          </form>
        </Card>

        <div className="space-y-4">
          <Card title="Quote">
            {!ticker ? (
              <p className="text-sm text-slate-500">Pick a symbol to see its live quote.</p>
            ) : !quote ? (
              <p className="text-sm text-slate-500">Loading {ticker}…</p>
            ) : (
              <div>
                <div className="flex items-baseline gap-3">
                  <span className="text-3xl font-semibold tabular-nums">{money(quote.price)}</span>
                  {quote.change_pct !== null && (
                    <span className={`inline-flex items-center gap-1 text-sm font-medium ${
                      quote.change_pct >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
                    }`}>
                      <span aria-hidden>{quote.change_pct >= 0 ? "▲" : "▼"}</span>
                      {pct(quote.change_pct)}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {quote.ticker} · {quote.provider} data · prev close {money(quote.prev_close)}
                </p>
                {selectedAsset && (
                  <div className="mt-3 space-y-1 border-t border-slate-800 pt-3 text-xs text-slate-400">
                    <div className="truncate text-slate-300">{selectedAsset.name}</div>
                    {selectedAsset.expense_ratio && (
                      <div>
                        Expense ratio:{" "}
                        <span className="tabular-nums text-slate-200">
                          {(parseFloat(selectedAsset.expense_ratio) * 100).toFixed(3).replace(/0+$/, "").replace(/\.$/, "")}%
                        </span>
                      </div>
                    )}
                    {selectedAsset.prospectus_url && (
                      <a href={selectedAsset.prospectus_url} target="_blank" rel="noopener noreferrer"
                         className="inline-block text-emerald-400 hover:text-emerald-300">
                        Prospectus filings (SEC EDGAR) ↗
                      </a>
                    )}
                  </div>
                )}
                {account && (
                  <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-400">
                    Buying power in {account.name}:{" "}
                    <span className="tabular-nums text-slate-200">
                      {money(account.buying_power ?? account.settlement_balance)}
                    </span>{" "}
                    of {money(account.settlement_balance)} in the settlement fund
                  </p>
                )}
              </div>
            )}
          </Card>

          {result && (
            <Card title="Order result">
              <div className="space-y-2 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-slate-400">Status</span>
                  <Badge value={result.order.status} />
                </div>
                {result.funding && <InfoText>{result.funding}</InfoText>}
                {result.order.status === "REJECTED" && (
                  <InfoText>{result.order.reject_reason}</InfoText>
                )}
                {result.transaction && (
                  <>
                    <div className="flex justify-between">
                      <span className="text-slate-400">Filled</span>
                      <span className="tabular-nums">
                        {fmtShares(result.transaction.shares_filled)} @ {money(result.transaction.executed_price)}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-400">Total</span>
                      <span className="tabular-nums">{money(result.transaction.gross_amount)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-400">Effective date</span>
                      <span>{shortDate(result.transaction.as_of)}</span>
                    </div>
                    {result.transaction.realized_gains != null && (
                      <div className="flex justify-between">
                        <span className="text-slate-400">Realized gains</span>
                        <span className={parseFloat(result.transaction.realized_gains) >= 0
                          ? "text-(--status-good)" : "text-(--status-critical)"}>
                          {money(result.transaction.realized_gains)}
                        </span>
                      </div>
                    )}
                  </>
                )}
                {result.order.status === "SCHEDULED" && (
                  <p className="text-xs text-slate-400">
                    {result.order.nav_date
                      ? `Queued — fills at the ${shortDate(result.order.nav_date)} closing NAV.`
                      : `Queued for ${dateTime(result.order.scheduled_for)}.`}
                  </p>
                )}
                {result.order.status === "PENDING" && (
                  <p className="text-xs text-slate-400">
                    Waiting for {result.order.ticker} to cross {money(result.order.limit_price)}
                    {result.order.expires_at && <> · expires {dateTime(result.order.expires_at)}</>}.
                  </p>
                )}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
