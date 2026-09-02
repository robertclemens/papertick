"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, MarketStatusT } from "@/lib/api";

/** Ceiling on how long a page waits before re-checking the session state. The
 *  hook normally wakes itself right after the next open or close instead, so
 *  this only matters for a long stretch in the middle of a session. Pure
 *  calendar arithmetic on the server — it never reaches a data provider. */
const STATUS_POLL_MS = 5 * 60 * 1000;

/** Auto-refresh for anything showing prices, gains, or account totals.
 *
 *  The cadence is decided by the server (`/market/status` → `refresh_seconds`)
 *  because that is where the trading calendar lives. Three regimes:
 *
 *    open   — prices are moving; re-price every MARKET_REFRESH_SECONDS.
 *    nav    — session over, but fund NAVs are still posting, so a portfolio's
 *             value can still change. Re-price slowly.
 *    closed — nothing can change until the next open, so nothing is fetched.
 *             The NYSE is shut for 81% of the week.
 *
 *  Polling also pauses while the tab is hidden and fires immediately when it
 *  becomes visible again, so a dashboard left open in a background tab costs
 *  nothing and is current the moment it is looked at.
 *
 *  Viewer count does not multiply upstream cost: quotes are served from one
 *  shared server-side cache, so the ceiling is (tickers / QUOTE_CACHE_SECONDS)
 *  no matter how many people are watching.
 */
export function useMarketRefresh(onRefresh: () => void): {
  status: MarketStatusT | null;
  lastRefresh: Date | null;
  refreshing: boolean;
  refreshNow: () => void;
} {
  const [status, setStatus] = useState<MarketStatusT | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  // kept in a ref so a re-created callback does not tear down the timer
  const cb = useRef(onRefresh);
  cb.current = onRefresh;

  const run = useCallback(() => {
    setRefreshing(true);
    try {
      cb.current();
    } finally {
      setLastRefresh(new Date());
      // the callback fires its own fetches; this is a brief activity hint,
      // not a completion signal, so the row never gets stuck on "updating"
      setTimeout(() => setRefreshing(false), 600);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    // Wake just after the next open or close so the status widget flips on its
    // own, instead of showing "closes in 0m" until the next fixed poll.
    const schedule = (s: MarketStatusT) => {
      const boundary = Date.parse(s.is_open ? s.next_close : s.next_open)
        - Date.parse(s.server_time);
      const wait = Math.min(STATUS_POLL_MS, Math.max(5_000, boundary + 5_000));
      timer = setTimeout(load, wait);
    };
    const load = () =>
      api<MarketStatusT>("/market/status")
        .then((s) => {
          if (!alive) return;
          setStatus(s);
          schedule(s);
        })
        .catch(() => { if (alive) timer = setTimeout(load, STATUS_POLL_MS); });
    load();
    return () => { alive = false; clearTimeout(timer); };
  }, []);

  const seconds = status?.refresh_seconds ?? 0;

  useEffect(() => {
    if (!seconds) return;
    const tick = () => {
      if (document.visibilityState === "visible") run();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") run();
    };
    const timer = setInterval(tick, seconds * 1000);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [seconds, run]);

  return { status, lastRefresh, refreshing, refreshNow: run };
}

const HOUR_MS = 60 * 60 * 1000;

/** "3h 12m" / "42 min" — coarse on purpose: the widget re-renders every 30s,
 *  so a seconds-precise countdown would be a lie between ticks. */
function countdown(ms: number): string {
  const mins = Math.max(0, Math.round(ms / 60_000));
  if (mins < 1) return "under a minute";
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

/** "Mon, 9:30 AM", in the reader's own timezone. */
function whenLabel(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    weekday: "short", hour: "numeric", minute: "2-digit",
  });
}

export interface MarketStatusView {
  /** live = prices are moving · settling = shut but fund NAVs still land ·
   *  shut = nothing can change before the next open */
  tone: "live" | "settling" | "shut";
  headline: string;
  detail: string;
}

/** What the status widget says, given the server's calendar answer and the
 *  current instant (already corrected for client clock skew by the caller). */
export function marketStatusView(status: MarketStatusT, nowMs: number): MarketStatusView {
  if (!status.enforce_market_hours) {
    // sandbox mode fills around the clock, so "closed" would be misleading
    return {
      tone: "live",
      headline: "Market hours off",
      detail: "orders fill at the latest price",
    };
  }
  if (status.is_open) {
    return {
      tone: "live",
      headline: "Market open",
      detail: `closes in ${countdown(Date.parse(status.next_close) - nowMs)}`,
    };
  }
  const opensIn = Date.parse(status.next_open) - nowMs;
  const opens = opensIn < HOUR_MS
    ? `opens in ${countdown(opensIn)}`
    : `opens ${whenLabel(status.next_open)}`;
  if (status.refresh_reason === "nav") {
    return {
      tone: "settling",
      headline: "Market closed",
      detail: `fund NAVs still posting · ${opens}`,
    };
  }
  return { tone: "shut", headline: "Market closed", detail: opens };
}
