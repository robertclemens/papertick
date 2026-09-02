"use client";

import { MarketStatusT } from "@/lib/api";
import { marketStatusView } from "@/lib/market-refresh";

import { ReactNode, useEffect, useState } from "react";

export function Card({ title, action, children, className = "" }: {
  title?: string; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <div className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="text-sm font-semibold text-slate-200">{title}</h2>}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, delta, deltaGood }: {
  label: string; value: string; delta?: string; deltaGood?: boolean | null;
}) {
  return (
    <div className="card">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold text-slate-50">{value}</div>
      {delta !== undefined && (
        <div
          className={`mt-1 inline-flex items-center gap-1 text-sm font-medium ${
            deltaGood === null || deltaGood === undefined
              ? "text-slate-400"
              : deltaGood
                ? "text-(--status-good)"
                : "text-(--status-critical)"
          }`}
        >
          {deltaGood !== null && deltaGood !== undefined && (
            <span aria-hidden>{deltaGood ? "▲" : "▼"}</span>
          )}
          {delta}
        </div>
      )}
    </div>
  );
}

const BADGE_STYLES: Record<string, string> = {
  FILLED: "bg-emerald-950/60 text-emerald-400 border-emerald-900",
  ACTIVE: "bg-emerald-950/60 text-emerald-400 border-emerald-900",
  SCHEDULED: "bg-sky-950/60 text-sky-400 border-sky-900",
  PENDING: "bg-amber-950/60 text-amber-400 border-amber-900",
  PAUSED: "bg-amber-950/60 text-amber-400 border-amber-900",
  REJECTED: "bg-red-950/60 text-red-400 border-red-900",
  CANCELLED: "bg-slate-800 text-slate-400 border-slate-700",
  EXPIRED: "bg-slate-800 text-slate-400 border-slate-700",
  BUY: "bg-emerald-950/60 text-emerald-400 border-emerald-900",
  SELL: "bg-sky-950/60 text-sky-400 border-sky-900",
  CONTRIBUTION: "bg-emerald-950/60 text-emerald-400 border-emerald-900",
  ROLLOVER: "bg-sky-950/60 text-sky-400 border-sky-900",
  WITHDRAWAL: "bg-amber-950/60 text-amber-400 border-amber-900",
  read: "bg-slate-800 text-slate-300 border-slate-700",
  trade: "bg-emerald-950/60 text-emerald-400 border-emerald-900",
};

export function Badge({ value }: { value: string }) {
  const style = BADGE_STYLES[value] ?? "bg-slate-800 text-slate-300 border-slate-700";
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${style}`}>
      {value}
    </span>
  );
}

export function Dialog({ open, title, onClose, children }: {
  open: boolean; title: string; onClose: () => void; children: ReactNode;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-base font-semibold text-slate-100">{title}</h3>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300" aria-label="Close">
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Confirmation for an action the user cannot take back. The consequence goes
 *  in `children` — the point is that it is stated, not that it is styled. */
export function ConfirmDialog({ open, title, confirmLabel, danger = true, busy, onConfirm, onClose, children }: {
  open: boolean;
  title: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <Dialog open={open} title={title} onClose={onClose}>
      <div className="space-y-4">
        <div className="space-y-2 text-sm text-slate-300">{children}</div>
        <div className="flex gap-2">
          <button type="button" className="btn-ghost flex-1" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={`${danger ? "btn-danger" : "btn-primary"} flex-1`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </Dialog>
  );
}


export function ErrorText({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p className="rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
      {children}
    </p>
  );
}

export function InfoText({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
      {children}
    </p>
  );
}

export function Spinner() {
  return (
    <div className="flex justify-center py-10" role="status" aria-label="Loading">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-slate-700 border-t-emerald-500" />
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center text-sm text-slate-500">{children}</div>;
}


/** Says that some of the fills behind the numbers on this page were entered for
 *  a date that had already happened. Stated plainly and without alarm: the point
 *  is that a return produced with hindsight is never presented as if it were a
 *  prediction that came good. Renders nothing when there are none. */
export function BackdatedNote({ count }: { count: number | undefined }) {
  if (!count) return null;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-amber-900 bg-amber-950/40 px-2.5 py-0.5 text-xs font-medium text-amber-300"
      title={
        `${count} of the trades behind these figures were entered after the date they ` +
        "filled on, so they were placed with the outcome already known."
      }
    >
      <span aria-hidden>◷</span>
      {count} past-dated fill{count === 1 ? "" : "s"}
    </span>
  );
}

const MARKET_TONE = {
  live:     { dot: "bg-emerald-400", box: "border-emerald-900 bg-emerald-950/40", text: "text-emerald-300" },
  settling: { dot: "bg-amber-400",   box: "border-amber-900 bg-amber-950/30",     text: "text-amber-200" },
  shut:     { dot: "bg-slate-500",   box: "border-slate-700 bg-slate-900/60",     text: "text-slate-300" },
} as const;

/** The one place a page says whether the market is open, how much of the
 *  session is left, and how fresh its prices are.
 *
 *  There is deliberately no "refresh now" control. While the market is open
 *  the page re-prices itself on the server's cadence and again the instant the
 *  tab is looked at; while it is shut there is nothing new to fetch, and a
 *  button that re-serves the same cached quote only teaches the reader to
 *  distrust the number. The single exception is a deployment that has turned
 *  auto-refresh off entirely (MARKET_REFRESH_SECONDS=0), where nothing else
 *  would ever move the numbers — there, and only there, the control earns its
 *  place. */
export function MarketStatus({ status, lastRefresh, refreshing, onRefresh }: {
  status: MarketStatusT | null;
  lastRefresh?: Date | null;
  refreshing?: boolean;
  onRefresh?: () => void;
}) {
  const [now, setNow] = useState(() => Date.now());
  // the countdown is quoted against the server's clock, not the browser's, so
  // a skewed laptop cannot invent an extra hour of trading
  const [skew, setSkew] = useState(0);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (status) setSkew(Date.parse(status.server_time) - Date.now());
  }, [status]);

  if (!status) return null;
  const view = marketStatusView(status, now + skew);
  const tone = MARKET_TONE[view.tone];
  const stalled = status.refresh_reason === "off";

  return (
    <div className={`mt-2 inline-flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border px-3 py-1.5 text-sm ${tone.box}`}>
      <span
        aria-hidden
        className={`h-2 w-2 shrink-0 rounded-full ${tone.dot} ${refreshing ? "animate-pulse" : ""}`}
      />
      <span className={`font-medium ${tone.text}`}>{view.headline}</span>
      <span className="text-slate-400">· {view.detail}</span>
      {lastRefresh && (
        <span className="text-xs text-slate-500">
          · updated {lastRefresh.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}
        </span>
      )}
      {stalled && onRefresh && (
        <button type="button" onClick={onRefresh}
                className="text-xs text-slate-400 underline underline-offset-2 hover:text-slate-200">
          Refresh
        </button>
      )}
    </div>
  );
}
