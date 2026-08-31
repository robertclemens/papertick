"use client";

import { useEffect, useState } from "react";
import { api, MeT, RangeT } from "@/lib/api";

/** The signed-in user's preferred performance window, fetched once per page
 *  load and shared by every timeframe picker. */
let cached: Promise<RangeT> | null = null;

export function defaultRange(): Promise<RangeT> {
  if (!cached) {
    cached = api<MeT>("/auth/me")
      .then((me) => me.default_range ?? "1y")
      .catch(() => "1y" as RangeT);
  }
  return cached;
}

/** Forget the cached preference after the user changes it in Settings. */
export function clearRangeCache(): void {
  cached = null;
}

/** `null` until the preference is known, so a page does not fetch a year of
 *  data at the default window and then immediately refetch at the user's. */
export function useRangePref(): [RangeT | null, (r: RangeT) => void] {
  const [range, setRange] = useState<RangeT | null>(null);
  useEffect(() => {
    let live = true;
    defaultRange().then((r) => { if (live) setRange((cur) => cur ?? r); });
    return () => { live = false; };
  }, []);
  return [range, setRange];
}
