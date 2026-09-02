export function money(v: string | number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!isFinite(n)) return "—";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });
}

export function signedMoney(v: string | number | null | undefined): string {
  const n = typeof v === "string" ? parseFloat(v) : (v ?? NaN);
  if (!isFinite(n)) return "—";
  return (n >= 0 ? "+" : "") + money(n);
}

export function pct(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(dp)}%`;
}

export function shares(v: string | number): string {
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!isFinite(n)) return "—";
  return n.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

export function expenseRatioPct(v: string | number | null | undefined): string | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!isFinite(n) || n <= 0) return null;
  const pctVal = n * 100;
  return `${parseFloat(pctVal.toFixed(4))}%`;
}

/** Date-only strings ("1975-03-10") are calendar dates, not instants: parse
 *  them in local time so they never shift a day west of UTC. */
function parseDate(iso: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (m) return new Date(+m[1], +m[2] - 1, +m[3]);
  return new Date(iso);
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return parseDate(iso).toLocaleDateString("en-US", {
    month: "short", day: "numeric", year: "numeric",
  });
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = parseDate(iso);
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

/** "2026-09" -> "Sep 2026". The month strip on the dashboard and the
 *  month-by-month table label the same row, so they share one formatter. */
export function monthLabel(month: string): string {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "short", year: "numeric" });
}
