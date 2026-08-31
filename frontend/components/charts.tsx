"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { PerformanceT, PositionT } from "@/lib/api";
import { money, shortDate } from "@/lib/format";
import { aggregateByTicker } from "@/lib/positions";

const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"];
const OTHER = "#64748b";
const SURFACE = "#0f172a";
const GRID = "#1e293b";
const MUTED = "#94a3b8";

function compactMoney(v: number): string {
  if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 1_000) return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs shadow-xl">
      {label && <div className="mb-1 font-medium text-slate-300">{shortDate(label)}</div>}
      {payload.map((p: any) => (
        <div key={p.name} className="flex items-center gap-2 py-0.5 text-slate-200">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: p.stroke || p.payload?.fill || p.color }}
          />
          <span className="text-slate-400">{p.name}:</span>
          <span className="font-medium tabular-nums">{money(p.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function PortfolioChart({ perf }: { perf: PerformanceT }) {
  const data = perf.series.map((p) => ({
    date: p.date,
    "Portfolio value": parseFloat(p.value),
    Invested: parseFloat(p.net_deposits),
  }));
  if (data.length < 2) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-slate-500">
        Not enough history yet — make a deposit or a trade to start the clock.
      </div>
    );
  }
  return (
    <div>
      <div className="h-64" role="img" aria-label="Portfolio value over time versus money invested">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id="valueFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3987e5" stopOpacity={0.28} />
                <stop offset="100%" stopColor="#3987e5" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={GRID} strokeDasharray="0" vertical={false} />
            <XAxis
              dataKey="date"
              tick={{ fill: MUTED, fontSize: 11 }}
              tickFormatter={(d) => shortDate(d)}
              axisLine={{ stroke: GRID }}
              tickLine={false}
              minTickGap={60}
            />
            <YAxis
              tick={{ fill: MUTED, fontSize: 11 }}
              tickFormatter={compactMoney}
              axisLine={false}
              tickLine={false}
              width={54}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: MUTED, strokeWidth: 1 }} />
            <Area
              type="monotone"
              dataKey="Invested"
              stroke={MUTED}
              strokeWidth={1.5}
              strokeDasharray="5 4"
              fill="none"
              dot={false}
              activeDot={{ r: 4, stroke: SURFACE, strokeWidth: 2 }}
            />
            <Area
              type="monotone"
              dataKey="Portfolio value"
              stroke="#3987e5"
              strokeWidth={2}
              fill="url(#valueFill)"
              dot={false}
              activeDot={{ r: 4, stroke: SURFACE, strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex items-center gap-5 text-xs text-slate-400">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className="inline-block h-0.5 w-4 rounded" style={{ background: "#3987e5" }} />
          Portfolio value
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-0.5 w-4 rounded"
            style={{ background: `repeating-linear-gradient(90deg, ${MUTED} 0 3px, transparent 3px 6px)` }}
          />
          Invested (beginning balance + net deposits)
        </span>
      </div>
    </div>
  );
}

export type AllocationGroupBy = "holding" | "category" | "region";

const CASH_FILL = "#334155";
const CASH_KEY = "__cash";
/** How many holdings get their own slice before the tail is rolled up. */
const TOP_HOLDINGS = 6;

// fixed color-per-entity assignments (never reshuffled by value rank)
const CATEGORY_META: Record<string, { label: string; fill: string }> = {
  STOCK: { label: "Stocks", fill: SERIES[0] },
  BOND: { label: "Bonds", fill: SERIES[2] },
  REAL_ESTATE: { label: "Real estate", fill: SERIES[1] },
  MIXED: { label: "Balanced", fill: SERIES[3] },
  COMMODITY: { label: "Commodities", fill: SERIES[4] },
  SHORT_TERM_RESERVES: { label: "Short-term reserves", fill: CASH_FILL },
  OTHER: { label: "Unclassified", fill: OTHER },
};
const REGION_META: Record<string, { label: string; fill: string }> = {
  US: { label: "U.S.", fill: SERIES[0] },
  INTERNATIONAL: { label: "International", fill: SERIES[1] },
  GLOBAL: { label: "Global", fill: SERIES[2] },
  OTHER: { label: "Unclassified", fill: OTHER },
};

interface Slice {
  /** stable identity for React and for colour assignment — never the label,
   *  which can repeat across groupings */
  key: string;
  name: string;
  value: number;
  fill: string;
}

/** Turn an unmapped enum value into something readable rather than dropping
 *  the money on the floor: an asset with a category this build does not know
 *  still has to appear somewhere. */
function titleCase(key: string): string {
  return key.charAt(0) + key.slice(1).toLowerCase().replace(/_/g, " ");
}

export function AllocationDonut({ positions, cash, groupBy = "holding" }: {
  positions: PositionT[]; cash: number; groupBy?: AllocationGroupBy;
}) {
  let slices: Slice[] = [];

  if (groupBy === "holding") {
    // One slice per symbol, not per position: the same fund held in three
    // accounts is one allocation, and three same-named slices also collide as
    // React keys, which is what made the chart go wrong when switching views.
    const sorted = aggregateByTicker(positions).filter((h) => h.market_value > 0.005);
    slices = sorted.slice(0, TOP_HOLDINGS).map((h, i) => ({
      key: h.ticker, name: h.ticker, value: h.market_value, fill: SERIES[i % SERIES.length],
    }));
    const tail = sorted.slice(TOP_HOLDINGS);
    const tailValue = tail.reduce((sum, h) => sum + h.market_value, 0);
    if (tailValue > 0.005) {
      slices.push({
        key: "__tail", name: `Other (${tail.length})`, value: tailValue, fill: OTHER,
      });
    }
  } else {
    const meta = groupBy === "category" ? CATEGORY_META : REGION_META;
    const sums = new Map<string, number>();
    for (const p of positions) {
      const key = (groupBy === "category" ? p.category : p.region) || "OTHER";
      sums.set(key, (sums.get(key) ?? 0) + parseFloat(p.market_value));
    }
    slices = Array.from(sums.entries())
      .filter(([, value]) => value > 0.005)
      .map(([key, value]) => ({
        key,
        name: meta[key]?.label ?? titleCase(key),
        value,
        fill: meta[key]?.fill ?? OTHER,
      }))
      .sort((a, b) => b.value - a.value);
  }

  // Uninvested money is "Cash" here — that is the asset class. The specific
  // holding behind it (the VMFXX settlement fund) is named on the account page.
  // It has no region, though, so it is left out of that breakdown entirely
  // rather than being lumped into a bucket it does not belong to.
  const showCash = groupBy !== "region" && cash > 0.005;
  if (showCash) {
    slices.push({ key: CASH_KEY, name: "Cash", value: cash, fill: CASH_FILL });
  }
  const total = slices.reduce((sum, s) => sum + s.value, 0);

  if (total <= 0) {
    return <div className="py-10 text-center text-sm text-slate-500">Nothing invested yet.</div>;
  }

  return (
    <div>
      <div className="mx-auto h-44 w-44" role="img" aria-label="Portfolio allocation">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Tooltip content={<ChartTooltip />} />
            <Pie
              data={slices}
              dataKey="value"
              nameKey="name"
              innerRadius={52}
              outerRadius={82}
              paddingAngle={2}
              stroke={SURFACE}
              strokeWidth={2}
              isAnimationActive={false}
            >
              {slices.map((s) => (
                <Cell key={s.key} fill={s.fill} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
      </div>
      {/* full-width legend below the donut: names get the whole card and are
          never squeezed into an ellipsis beside it */}
      <ul className="mt-4 space-y-1.5 border-t border-slate-800 pt-3 text-sm">
        {slices.map((s) => (
          <li key={s.key} className="flex items-center gap-2">
            <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: s.fill }} />
            <span className="min-w-0 flex-1 font-medium text-slate-200">{s.name}</span>
            <span className="shrink-0 tabular-nums text-slate-400">{money(s.value)}</span>
            <span className="w-12 shrink-0 text-right tabular-nums text-slate-500">
              {((s.value / total) * 100).toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
      {showCash ? (
        <p className="mt-2 text-xs text-slate-500">
          Cash is held in your settlement fund (VMFXX).
        </p>
      ) : groupBy === "region" && cash > 0.005 ? (
        <p className="mt-2 text-xs text-slate-500">
          Invested holdings only — {money(cash)} of cash has no region.
        </p>
      ) : null}
    </div>
  );
}
