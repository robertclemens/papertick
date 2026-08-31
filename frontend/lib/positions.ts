import { PositionT } from "@/lib/api";

/** One holding, summed across every account that holds it.
 *
 *  The API returns positions per account, which is right for an account view
 *  and wrong for a portfolio one: the same fund in three accounts is one
 *  holding, not three. Average cost is re-derived from the pooled basis, so it
 *  is the blended cost of everything owned rather than any single account's. */
export interface HoldingT {
  ticker: string;
  name: string;
  asset_class: string;
  category: string;
  region: string;
  expense_ratio: string | null;
  prospectus_url: string | null;
  shares: number;
  cost_basis: number;
  market_value: number;
  average_cost: number;
  price: number;
  unrealized_gains: number;
  unrealized_gains_pct: number | null;
  /** account ids holding it — length > 1 means the row is a roll-up */
  account_ids: string[];
}

export function aggregateByTicker(positions: PositionT[]): HoldingT[] {
  const byTicker = new Map<string, HoldingT>();
  for (const p of positions) {
    const shares = parseFloat(p.shares);
    const cost = parseFloat(p.cost_basis);
    const value = parseFloat(p.market_value);
    const existing = byTicker.get(p.ticker);
    if (existing) {
      existing.shares += shares;
      existing.cost_basis += cost;
      existing.market_value += value;
      existing.account_ids.push(p.account_id);
    } else {
      byTicker.set(p.ticker, {
        ticker: p.ticker,
        name: p.name,
        asset_class: p.asset_class,
        category: p.category,
        region: p.region,
        expense_ratio: p.expense_ratio,
        prospectus_url: p.prospectus_url,
        shares,
        cost_basis: cost,
        market_value: value,
        average_cost: 0,
        price: parseFloat(p.price),
        unrealized_gains: 0,
        unrealized_gains_pct: null,
        account_ids: [p.account_id],
      });
    }
  }
  const out = Array.from(byTicker.values());
  for (const h of out) {
    h.average_cost = h.shares > 0 ? h.cost_basis / h.shares : 0;
    h.unrealized_gains = h.market_value - h.cost_basis;
    h.unrealized_gains_pct = h.cost_basis > 0 ? (h.unrealized_gains / h.cost_basis) * 100 : null;
  }
  return out.sort((a, b) => b.market_value - a.market_value);
}
