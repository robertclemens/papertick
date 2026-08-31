"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { getScenario, ScenarioT, setScenario } from "@/lib/api";
import { useScenarios } from "@/components/scenario-context";

/** Which track of data the whole app is looking at.
 *
 *  Switching reloads rather than re-fetching piecemeal: every page holds
 *  scenario-scoped state, and a hard reload is the only way to guarantee none
 *  of it survives into the new track. */
export default function ScenarioSwitcher() {
  const router = useRouter();
  // shared with the scenarios page, so creating or deleting one updates the
  // switcher immediately instead of waiting for a reload
  const { scenarios } = useScenarios();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  if (!scenarios || scenarios.length === 0) return null;

  const activeId = getScenario();
  const active = scenarios.find((s) => s.id === activeId)
    ?? scenarios.find((s) => s.is_active)
    ?? scenarios.find((s) => s.is_default)
    ?? scenarios[0];

  function pick(s: ScenarioT) {
    setOpen(false);
    if (s.id === active.id) return;
    setScenario(s.id);
    // full reload: nothing from the previous scenario should survive
    window.location.reload();
  }

  return (
    <div ref={rootRef} className="relative px-3 pb-1">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2 text-left transition hover:border-slate-700 hover:bg-slate-800/50"
      >
        <span aria-hidden className="text-xs text-emerald-400">◆</span>
        <span className="min-w-0 flex-1">
          <span className="block text-[10px] uppercase tracking-wide text-slate-500">
            Scenario
          </span>
          <span className="block truncate text-sm font-medium text-slate-200" title={active.name}>
            {active.name}
          </span>
        </span>
        <span aria-hidden className="shrink-0 text-xs text-slate-500">{open ? "▲" : "▾"}</span>
      </button>

      {open && (
        <ul role="listbox"
            className="absolute bottom-full left-3 right-3 z-40 mb-1 max-h-80 overflow-auto rounded-lg border border-slate-700 bg-slate-900 py-1 shadow-2xl">
          {scenarios.map((s) => (
            <li key={s.id} role="option" aria-selected={s.id === active.id}>
              <button
                onClick={() => pick(s)}
                className={`flex w-full items-start gap-2 px-3 py-2 text-left text-sm transition ${
                  s.id === active.id ? "bg-slate-800 text-slate-100" : "text-slate-300 hover:bg-slate-800/60"
                }`}
              >
                <span aria-hidden className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                  s.id === active.id ? "bg-emerald-400" : "bg-transparent"
                }`} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{s.name}</span>
                  <span className="block text-xs text-slate-500">
                    {s.account_count} account{s.account_count === 1 ? "" : "s"}
                    {s.is_default && " · default"}
                  </span>
                </span>
              </button>
            </li>
          ))}
          <li className="mt-1 border-t border-slate-800 pt-1">
            <Link href="/scenarios" onClick={() => setOpen(false)}
                  className="block px-3 py-2 text-sm text-emerald-400 hover:bg-slate-800/60">
              Manage scenarios →
            </Link>
          </li>
        </ul>
      )}
    </div>
  );
}
