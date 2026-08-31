"use client";

import { ReactNode } from "react";

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
