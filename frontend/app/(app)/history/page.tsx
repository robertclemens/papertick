"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AccountT,
  api,
  ApiError,
  DividendT,
  download,
  OrderT,
  RANGE_LABEL,
  RANGES,
  RangeT,
  TransactionT,
  withinRange,
} from "@/lib/api";
import { dateTime, money, shares, shortDate, signedMoney } from "@/lib/format";
import { Badge, Card, Empty, ErrorText, Spinner } from "@/components/ui";
import { useRangePref } from "@/lib/prefs";

type Tab = "orders" | "transactions" | "dividends";

// The window filters the column each table leads with, so the rows on screen
// and the rows in the export are the same set.
const DATE_FIELD: Record<Tab, string> = {
  orders: "when it was placed",
  transactions: "when it executed",
  dividends: "ex-date",
};

/** "all", or an account id. Kept out of the fetch: every row already carries
 *  its account_id, so filtering client-side keeps the tab switch instant and
 *  the row cap honest — the cap applies to the whole scenario either way, so
 *  narrowing after the fetch never hides rows a narrowed fetch would show. */
type AccountFilter = string;

export default function HistoryPage() {
  const [tab, setTab] = useState<Tab>("orders");
  const [range, setRange] = useRangePref();
  const [accountId, setAccountId] = useState<AccountFilter>("all");
  const [accounts, setAccounts] = useState<AccountT[] | null>(null);
  const [orders, setOrders] = useState<OrderT[] | null>(null);
  const [txns, setTxns] = useState<TransactionT[] | null>(null);
  const [divs, setDivs] = useState<DividendT[] | null>(null);
  const [exporting, setExporting] = useState("");
  const [exportError, setExportError] = useState("");

  const ORDER_CAP = 500;
  const DIVIDEND_CAP = 1000;

  function loadOrders() {
    api<OrderT[]>(`/orders?limit=${ORDER_CAP}`).then(setOrders).catch(() => setOrders([]));
  }

  useEffect(() => {
    loadOrders();
    api<AccountT[]>("/accounts").then(setAccounts).catch(() => setAccounts([]));
    api<TransactionT[]>(`/transactions?limit=${ORDER_CAP}`).then(setTxns).catch(() => setTxns([]));
    api<DividendT[]>(`/portfolio/dividends?limit=${DIVIDEND_CAP}`).then(setDivs).catch(() => setDivs([]));
  }, []);

  /** id -> display name, so every row can name the bucket it happened in. */
  const accountName = useMemo(() => {
    const map = new Map<string, string>();
    (accounts ?? []).forEach((a) => map.set(a.id, a.name));
    return (id: string) => map.get(id) ?? "—";
  }, [accounts]);

  const inAccount = (id: string) => accountId === "all" || id === accountId;

  async function cancel(id: string) {
    try {
      await api(`/orders/${id}`, { method: "DELETE" });
      loadOrders();
    } catch { /* refresh below regardless */ }
  }

  const shownOrders = useMemo(
    () => (orders ?? []).filter((o) => inAccount(o.account_id) && (!range || withinRange(o.created_at, range))),
    [orders, range, accountId]);
  const shownTxns = useMemo(
    () => (txns ?? []).filter((t) => inAccount(t.account_id) && (!range || withinRange(t.executed_at, range))),
    [txns, range, accountId]);
  const shownDivs = useMemo(
    () => (divs ?? []).filter((d) => inAccount(d.account_id) && (!range || withinRange(d.event_date, range))),
    [divs, range, accountId]);

  const loaded = { orders, transactions: txns, dividends: divs }[tab];
  const rowCount = { orders: shownOrders, transactions: shownTxns, dividends: shownDivs }[tab].length;
  const capped =
    (tab === "dividends" ? (divs?.length ?? 0) >= DIVIDEND_CAP : (loaded?.length ?? 0) >= ORDER_CAP);

  async function exportAs(fmt: "csv" | "xlsx") {
    setExportError("");
    setExporting(fmt);
    try {
      // the export mirrors what is on screen, account filter included
      const scope = accountId === "all" ? "" : `&account_id=${encodeURIComponent(accountId)}`;
      await download(`/export/${tab}.${fmt}?range=${range ?? "1y"}${scope}`,
                     `papertick-${tab}.${fmt}`);
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Export failed");
    } finally {
      setExporting("");
    }
  }

  const tabBtn = (active: boolean) =>
    `rounded-md px-3 py-1.5 text-sm font-medium transition ${
      active ? "bg-slate-700 text-slate-100" : "text-slate-400 hover:text-slate-200"
    }`;

  return (
    <div className="space-y-6">
      <header className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">History</h1>
            <p className="mt-1 text-sm text-slate-400">Every order and executed transaction, timestamped.</p>
          </div>
          <div className="flex gap-1 rounded-lg border border-slate-700 bg-slate-950 p-1" role="group" aria-label="History view">
            <button className={tabBtn(tab === "orders")} onClick={() => setTab("orders")}>Orders</button>
            <button className={tabBtn(tab === "transactions")} onClick={() => setTab("transactions")}>Transactions</button>
            <button className={tabBtn(tab === "dividends")} onClick={() => setTab("dividends")}>Dividends</button>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-800 pt-3">
          <div className="flex items-center gap-3">
            <div className="flex flex-wrap gap-1" role="group" aria-label="History timeframe">
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
            <select
              className="input !w-auto !py-1 text-xs"
              aria-label="Filter by account"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
            >
              <option value="all">All accounts</option>
              {(accounts ?? []).map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </select>
            <span className="text-xs text-slate-500">
              {rowCount} {rowCount === 1 ? "row" : "rows"}
              {range && ` · ${RANGE_LABEL[range].toLowerCase()}`} by {DATE_FIELD[tab]}
              {accountId !== "all" && ` · ${accountName(accountId)}`}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">Export this view</span>
            <button className="btn-ghost !py-1.5 text-xs" disabled={!!exporting}
                    onClick={() => exportAs("csv")}>
              {exporting === "csv" ? "Preparing…" : "CSV"}
            </button>
            <button className="btn-ghost !py-1.5 text-xs" disabled={!!exporting}
                    onClick={() => exportAs("xlsx")}>
              {exporting === "xlsx" ? "Preparing…" : "Excel"}
            </button>
          </div>
        </div>
        <ErrorText>{exportError}</ErrorText>
        {capped && (
          <p className="text-xs text-slate-500">
            The table shows the most recent {tab === "dividends" ? DIVIDEND_CAP : ORDER_CAP}{" "}
            records; exports always contain every row in the selected timeframe.
          </p>
        )}
      </header>

      {tab === "orders" ? (
        <Card>
          {!orders ? (
            <Spinner />
          ) : shownOrders.length === 0 ? (
            <Empty>No orders in this timeframe{accountId !== "all" ? ` for ${accountName(accountId)}` : ""}.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-base">
                <thead>
                  <tr>
                    <th>Placed</th><th>Account</th><th>Side</th><th>Ticker</th><th>Type</th>
                    <th>Quantity</th><th>Status</th><th>Detail</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {shownOrders.map((o) => (
                    <tr key={o.id}>
                      <td className="whitespace-nowrap">{dateTime(o.created_at)}</td>
                      <td className="whitespace-nowrap text-xs text-slate-400">{accountName(o.account_id)}</td>
                      <td><Badge value={o.side} /></td>
                      <td className="font-medium">{o.ticker}</td>
                      <td className="text-xs text-slate-400">
                        {o.order_type}
                        {o.limit_price && ` @ ${money(o.limit_price)}`}
                        {o.as_of && ` · backtest ${shortDate(o.as_of)}`}
                        {o.scheduled_for && ` · for ${dateTime(o.scheduled_for)}`}
                        {o.status === "PENDING" && o.expires_at && ` · expires ${shortDate(o.expires_at)}`}
                        {o.source === "RECURRING" && " · auto"}
                      </td>
                      <td>
                        {o.quantity_type === "DOLLARS" ? money(o.quantity) : `${shares(o.quantity)} sh`}
                      </td>
                      <td><Badge value={o.status} /></td>
                      <td className="max-w-56 truncate text-xs text-slate-500" title={o.reject_reason ?? ""}>
                        {o.reject_reason ?? "—"}
                      </td>
                      <td>
                        {(o.status === "PENDING" || o.status === "SCHEDULED") && (
                          <button className="text-xs text-red-400 hover:text-red-300" onClick={() => cancel(o.id)}>
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ) : tab === "dividends" ? (
        <Card>
          {!divs ? (
            <Spinner />
          ) : shownDivs.length === 0 ? (
            <Empty>No dividends in this timeframe — dividends accrue for shares held on each ex-date.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-base">
                <thead>
                  <tr>
                    <th>Ex-date</th><th>Account</th><th>Ticker</th><th>Per share</th>
                    <th>Shares held</th><th>Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {shownDivs.map((d) => (
                    <tr key={d.id}>
                      <td>{shortDate(d.event_date)}</td>
                      <td className="whitespace-nowrap text-xs text-slate-400">{accountName(d.account_id)}</td>
                      <td className="font-medium">{d.ticker}</td>
                      <td>{money(d.per_share, 4)}</td>
                      <td>{shares(d.shares)}</td>
                      <td className="text-(--status-good)">{signedMoney(d.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ) : (
        <Card>
          {!txns ? (
            <Spinner />
          ) : shownTxns.length === 0 ? (
            <Empty>No transactions in this timeframe{accountId !== "all" ? ` for ${accountName(accountId)}` : ""}.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-base">
                <thead>
                  <tr>
                    <th>Executed</th><th>Effective</th><th>Account</th><th>Side</th><th>Ticker</th>
                    <th>Shares</th><th>Price</th><th>Amount</th><th>Fees</th><th>Realized gains</th>
                  </tr>
                </thead>
                <tbody>
                  {shownTxns.map((t) => (
                    <tr key={t.id}>
                      <td className="whitespace-nowrap text-xs">{dateTime(t.executed_at)}</td>
                      <td>{shortDate(t.as_of)}</td>
                      <td className="whitespace-nowrap text-xs text-slate-400">{accountName(t.account_id)}</td>
                      <td><Badge value={t.side} /></td>
                      <td className="font-medium">{t.ticker}</td>
                      <td>{shares(t.shares_filled)}</td>
                      <td>{money(t.executed_price)}</td>
                      <td>{money(t.gross_amount)}</td>
                      <td>{money(t.fees)}</td>
                      <td className={
                        t.realized_gains == null ? "text-slate-500"
                        : parseFloat(t.realized_gains) >= 0 ? "text-(--status-good)" : "text-(--status-critical)"
                      }>
                        {t.realized_gains == null ? "—" : signedMoney(t.realized_gains)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
