"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

interface ResultT {
  ticker: string;
  name: string;
  type: string;
  registered: boolean;
  exchange?: string;
}

/** Autocomplete that searches tickers AND company/fund names. */
export default function SymbolSearch({ value, onSelect, id, placeholder }: {
  value: string;
  onSelect: (ticker: string) => void;
  id?: string;
  placeholder?: string;
}) {
  const [query, setQuery] = useState(value);
  const [results, setResults] = useState<ResultT[]>([]);
  // the query these results describe — guards against acting on a stale list
  const [resultsFor, setResultsFor] = useState("");
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const skipNext = useRef(false);
  // The suggestion list belongs to typing, not to having a value. Editing an
  // existing record seeds the field with a symbol that is already chosen —
  // popping a dropdown over it just gets in the way.
  const [typing, setTyping] = useState(false);

  useEffect(() => {
    // Ignore the echo of our own onSelect (the field emits on every keystroke);
    // only a value seeded from outside — editing an existing record — resets
    // the field and keeps the suggestion list closed.
    if (value === query.trim().toUpperCase()) return;
    setQuery(value);
    setTyping(false);
    setOpen(false);
    if (value) skipNext.current = true;  // already a chosen symbol: no lookup
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  useEffect(() => {
    if (skipNext.current) {
      skipNext.current = false;
      return;
    }
    const q = query.trim();
    if (q.length < 1) {
      setResults([]);
      setResultsFor("");
      setOpen(false);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      api<ResultT[]>(`/market/search?q=${encodeURIComponent(q)}`)
        .then((rows) => {
          if (cancelled) return;   // a newer keystroke already superseded this
          setResults(rows);
          setResultsFor(q);
          setOpen(rows.length > 0 && typing);
          setHighlight(0);
        })
        .catch(() => {
          if (!cancelled) {
            setResults([]);
            setResultsFor(q);
          }
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [query]);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function pick(r: ResultT) {
    skipNext.current = true;
    setQuery(r.ticker);
    setResultsFor(r.ticker);
    setOpen(false);
    setTyping(false);
    onSelect(r.ticker);
  }

  /** Enter commits what the user typed unless the visible list is both fresh
   *  and genuinely their choice — otherwise typing NVDA and hitting Enter
   *  before the new results land would select a stale NVD row. */
  function commitOnEnter() {
    const typed = query.trim().toUpperCase();
    const fresh = resultsFor === query.trim();
    if (fresh) {
      const exact = results.find((r) => r.ticker === typed);
      if (exact) return pick(exact);
      const hit = results[highlight];
      if (hit) return pick(hit);
    }
    setOpen(false);
    setTyping(false);
    onSelect(typed);
  }

  return (
    <div ref={rootRef} className="relative">
      <input
        id={id}
        className="input uppercase"
        placeholder={placeholder ?? "Ticker or company name…"}
        value={query}
        autoComplete="off"
        onChange={(e) => {
          setTyping(true);
          setQuery(e.target.value);
          onSelect(e.target.value.trim().toUpperCase());
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); commitOnEnter(); return; }
          if (!open) return;
          if (e.key === "ArrowDown") { e.preventDefault(); setHighlight((h) => Math.min(h + 1, results.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setHighlight((h) => Math.max(h - 1, 0)); }
          else if (e.key === "Escape") setOpen(false);
        }}
        onFocus={() => typing && results.length > 0 && setOpen(true)}
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
      />
      {open && (
        <ul className="absolute z-30 mt-1 max-h-72 w-full min-w-72 overflow-auto rounded-lg border border-slate-700 bg-slate-900 py-1 shadow-2xl"
            role="listbox">
          {results.map((r, i) => (
            <li key={r.ticker} role="option" aria-selected={i === highlight}>
              <button
                type="button"
                className={`flex w-full items-center gap-3 px-3 py-2 text-left text-sm ${
                  i === highlight ? "bg-slate-800" : "hover:bg-slate-800/60"
                }`}
                onMouseEnter={() => setHighlight(i)}
                onClick={() => pick(r)}
              >
                <span className="w-16 shrink-0 font-semibold text-emerald-400">{r.ticker}</span>
                <span className="min-w-0 flex-1 truncate text-slate-200">{r.name}</span>
                <span className="shrink-0 text-xs text-slate-500">
                  {r.type === "MUTUALFUND" || r.type === "MUTUAL_FUND" ? "Fund" : r.type === "ETF" ? "ETF" : "Stock"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
