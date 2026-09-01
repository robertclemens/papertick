"use client";

import { withBasePath } from "@/lib/base-path";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** The scenario every request reads and writes. Kept in localStorage so a
 *  reload stays put; the server falls back to the user's default when the
 *  header is absent, so an unset value is always safe. */
const SCENARIO_KEY = "pt_scenario";
let activeScenario: string | null = null;

export function getScenario(): string | null {
  if (activeScenario) return activeScenario;
  try {
    activeScenario = localStorage.getItem(SCENARIO_KEY);
  } catch {
    activeScenario = null;   // private mode / storage disabled
  }
  return activeScenario;
}

export function setScenario(id: string | null): void {
  activeScenario = id;
  try {
    if (id) localStorage.setItem(SCENARIO_KEY, id);
    else localStorage.removeItem(SCENARIO_KEY);
  } catch { /* storage unavailable — the server default still applies */ }
}

let refreshing: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  if (!refreshing) {
    refreshing = fetch(withBasePath("/api/v1/auth/refresh"), { method: "POST", credentials: "include" })
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => setTimeout(() => (refreshing = null), 0));
  }
  return refreshing;
}

export async function api<T = any>(
  path: string,
  options: { method?: string; body?: any; retry?: boolean } = {}
): Promise<T> {
  const { method = "GET", body, retry = true } = options;
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const scenario = getScenario();
  if (scenario) headers["X-Scenario-Id"] = scenario;
  const res = await fetch(withBasePath(`/api/v1${path}`), {
    method,
    credentials: "include",
    headers: Object.keys(headers).length ? headers : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && retry && !path.startsWith("/auth/login") && !path.startsWith("/auth/signup")) {
    if (await tryRefresh()) {
      return api<T>(path, { method, body, retry: false });
    }
    if (typeof window !== "undefined") window.location.href = withBasePath("/login");
    throw new ApiError(401, "Session expired");
  }
  if (res.status === 204) return undefined as T;
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON body */
  }
  if (!res.ok) {
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : Array.isArray(data?.detail)
          ? data.detail.map((d: any) => d.msg).join("; ")
          : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

/** Fetch a file behind the session cookie and hand it to the browser as a
 *  download. Goes through the same refresh dance as `api()` so a long-open tab
 *  does not silently 401 into an empty file. */
export async function download(path: string, fallbackName: string): Promise<void> {
  const scenario = getScenario();
  const init: RequestInit = {
    credentials: "include",
    headers: scenario ? { "X-Scenario-Id": scenario } : undefined,
  };
  let res = await fetch(withBasePath(`/api/v1${path}`), init);
  if (res.status === 401) {
    if (!(await tryRefresh())) {
      if (typeof window !== "undefined") window.location.href = withBasePath("/login");
      throw new ApiError(401, "Session expired");
    }
    res = await fetch(withBasePath(`/api/v1${path}`), init);
  }
  if (!res.ok) throw new ApiError(res.status, `Export failed (${res.status})`);

  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = match ? match[1] : fallbackName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- shared response shapes (mirrors backend schemas) ----

export type AccountTypeT = "TAXABLE" | "ROTH_IRA" | "TRADITIONAL_IRA" | "ROLLOVER_IRA";

/** Annual IRA contribution progress for one tax year. The limit is shared
 *  across all of a user's IRAs, so `contributed` is the household total and
 *  `contributed_here` is this account's share. Between Jan 1 and Tax Day the
 *  prior year is still fundable, so an account can have two live buckets. */
export interface ContributionStatusT {
  tax_year: number;
  limit: string;
  contributed: string;
  contributed_here: string;
  remaining: string;
  used_pct: number;
  catchup_included: boolean;
  is_prior_year: boolean;
  designation_deadline: string | null;
}

export interface TaxYearBucketT {
  tax_year: number;
  remaining: string;
  designation_deadline: string;
  is_prior_year: boolean;
}

export interface AllowedYearsT {
  allowed_tax_years: number[];
  default_tax_year: number;
  buckets: TaxYearBucketT[];
}

export interface AccountT {
  id: string;
  account_type: AccountTypeT;
  name: string;
  /** uninvested cash, held in the settlement fund (VMFXX) */
  settlement_balance: string;
  settlement_ticker: string;
  settlement_name: string;
  settlement_yield: string | null;
  settlement_accrued: string | null;
  cost_basis_method: string;
  allow_external_funding: boolean;
  buying_power: string | null;
  contribution_statuses: ContributionStatusT[];
  created_at: string;
}

export interface SettlementT {
  account_id: string;
  account_name: string;
  ticker: string;
  name: string;
  balance: string;
  shares: string;
  nav: string;
  accrued_dividend: string;
  seven_day_yield: string;
}

export interface PositionT {
  account_id: string;
  ticker: string;
  name: string;
  asset_class: string;
  category: "STOCK" | "BOND" | "REAL_ESTATE" | "MIXED" | "OTHER";
  region: "US" | "INTERNATIONAL" | "GLOBAL" | "OTHER";
  expense_ratio: string | null;
  prospectus_url: string | null;
  shares: string;
  average_cost: string;
  cost_basis: string;
  price: string;
  market_value: string;
  unrealized_gains: string;
  unrealized_gains_pct: number | null;
}

export interface SummaryT {
  total_value: string;
  cash: string;
  reserved_cash: string;
  committed_cash: string;
  available_to_trade: string;
  open_order_count: number;
  invested_value: string;
  options_value: string;
  cost_basis: string;
  net_deposits: string;
  unrealized_gains: string;
  realized_gains: string;
  /** realized in a brokerage account — the part with a tax consequence */
  realized_gains_taxable: string;
  /** realized inside an IRA — bookkeeping only, never taxed or reported */
  realized_gains_sheltered: string;
  total_dividends: string;
  total_fees: string;
  accounts: AccountT[];
}

/** Every figure is scoped to the requested range, so it tracks the timeframe
 *  picker rather than reporting since-inception numbers. */
export interface PerformanceT {
  series: { date: string; value: string; net_deposits: string }[];
  twr_pct: number | null;
  irr_pct: number | null;
  rate_of_return_pct: number | null;
  rate_of_return_annualized: boolean;
  beginning_balance: string;
  ending_balance: string;
  net_cash_flow: string;
  investment_returns: string;
  dividends: string;
  period_start: string | null;
  period_end: string | null;
}

export const EMPTY_PERFORMANCE: PerformanceT = {
  series: [],
  twr_pct: null,
  irr_pct: null,
  rate_of_return_pct: null,
  rate_of_return_annualized: false,
  beginning_balance: "0.00",
  ending_balance: "0.00",
  net_cash_flow: "0.00",
  investment_returns: "0.00",
  dividends: "0.00",
  period_start: null,
  period_end: null,
};

export interface AccountReturnT {
  account_id: string;
  name: string;
  account_type: AccountTypeT;
  balance: string;
  settlement_balance: string;
  investment_returns: string;
  rate_of_return_pct: number | null;
  rate_of_return_annualized: boolean;
}

export interface AccountReturnsT {
  range: string;
  period_start: string | null;
  period_end: string | null;
  accounts: AccountReturnT[];
  total_balance: string;
  total_investment_returns: string;
  total_rate_of_return_pct: number | null;
}

export const RANGES = ["1m", "3m", "6m", "1y", "5y", "10y", "all"] as const;
export type RangeT = (typeof RANGES)[number];

/** Same windows the API uses (app/services/metrics.py, app/services/exports.py),
 *  so a filtered table and its export always cover the same days. */
export const RANGE_DAYS: Record<RangeT, number | null> = {
  "1m": 31, "3m": 92, "6m": 183, "1y": 366, "5y": 1827, "10y": 3653, all: null,
};

export function rangeStart(range: RangeT): Date | null {
  const days = RANGE_DAYS[range];
  if (days === null) return null;
  const d = new Date();
  d.setDate(d.getDate() - days);
  d.setHours(0, 0, 0, 0);
  return d;
}

export function withinRange(iso: string | null | undefined, range: RangeT): boolean {
  const start = rangeStart(range);
  if (!start || !iso) return true;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  const when = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(iso);
  return !isNaN(when.getTime()) && when >= start;
}

export const RANGE_LABEL: Record<RangeT, string> = {
  "1m": "1 month",
  "3m": "3 months",
  "6m": "6 months",
  "1y": "1 year",
  "5y": "5 years",
  "10y": "10 years",
  all: "Since inception",
};

export interface OrderT {
  id: string;
  account_id: string;
  ticker: string;
  side: "BUY" | "SELL";
  order_type: "MARKET" | "LIMIT";
  quantity_type: "SHARES" | "DOLLARS";
  quantity: string;
  limit_price: string | null;
  status: "PENDING" | "SCHEDULED" | "FILLED" | "REJECTED" | "CANCELLED" | "EXPIRED";
  time_in_force: string | null;
  expires_at: string | null;
  scheduled_for: string | null;
  as_of: string | null;
  nav_date: string | null;
  reject_reason: string | null;
  source: string;
  created_at: string;
}

export interface TransactionT {
  id: string;
  order_id: string;
  account_id: string;
  ticker: string;
  side: "BUY" | "SELL";
  executed_price: string;
  shares_filled: string;
  gross_amount: string;
  fees: string;
  realized_gains: string | null;
  realized_st: string | null;
  realized_lt: string | null;
  as_of: string;
  executed_at: string;
}

export interface DividendT {
  id: string;
  account_id: string;
  ticker: string;
  event_date: string;
  per_share: string;
  shares: string;
  amount: string;
}

export interface MarketStatusT {
  is_open: boolean;
  is_trading_day: boolean;
  next_open: string;
  next_close: string;
  enforce_market_hours: boolean;
  /** past-dated ("as of") fills; off unless ALLOW_BACKDATED_TRADES is set */
  allow_backdated_trades: boolean;
  server_time: string;
}

export type CostBasisMethodT = "FIFO" | "LIFO" | "HIFO" | "MIN_TAX" | "AVERAGE" | "SPEC_ID";

export const COST_BASIS_LABEL: Record<CostBasisMethodT, string> = {
  FIFO: "FIFO — first in, first out",
  LIFO: "LIFO — last in, first out",
  HIFO: "HIFO — highest cost first",
  MIN_TAX: "MinTax — losses first, gains last",
  AVERAGE: "Average cost (mutual funds only)",
  SPEC_ID: "Specific lot identification",
};

export interface LotT {
  id: string;
  account_id: string;
  ticker: string;
  shares_open: string;
  cost_per_share: string;
  cost_basis: string;
  acquired_on: string;
  term: "SHORT" | "LONG";
  price: string | null;
  unrealized_gains: string | null;
}

export interface CostBasisConfigT {
  account_id: string;
  default_method: CostBasisMethodT;
  overrides: { ticker: string; method: CostBasisMethodT; average_locked: boolean }[];
  notes: string[];
}

export interface OptionQuoteT {
  bid: string;
  ask: string;
  mid: string;
  iv: number;
  delta: number;
  theta: number;
  itm: boolean;
}

export interface ChainT {
  underlying: string;
  spot: string;
  expiry: string;
  days_to_expiry: number;
  rows: { strike: string; call: OptionQuoteT; put: OptionQuoteT }[];
  pricing_model: string;
}

export interface OptionPositionT {
  id: string;
  account_id: string;
  underlying: string;
  right: "CALL" | "PUT";
  strike: string;
  expiry: string;
  side: "LONG" | "SHORT";
  contracts: number;
  avg_premium: string;
  collateral: string;
  opened_on: string;
}

export interface OptionPositionViewT {
  position: OptionPositionT;
  mark: string;
  market_value: string;
  unrealized_gains: string;
  underlying_price: string;
  days_to_expiry: number;
  itm: boolean;
}

export interface OptionTransactionT {
  id: string;
  underlying: string;
  right: "CALL" | "PUT";
  strike: string;
  expiry: string;
  action: string;
  contracts: number;
  premium: string;
  cash_effect: string;
  realized_gains: string | null;
  as_of: string;
  executed_at: string;
}

export interface StatementT {
  id: string;
  kind: "MONTHLY" | "YEAR_END";
  period_start: string;
  period_end: string;
  generated_at: string;
}

export interface TaxReportT {
  year: number;
  account_id: string | null;
  short_term_gains: string;
  long_term_gains: string;
  unclassified_gains: string;
  dividends: string;
  fees: string;
  traditional_withdrawals: string;
  roth_withdrawals: string;
  ira_contributions: string;
  rollovers: string;
  notes: string[];
}

export interface ScheduleT {
  id: string;
  account_id: string;
  ticker: string;
  amount: string;
  cadence: "DAILY" | "WEEKLY" | "BIWEEKLY" | "MONTHLY" | "QUARTERLY" | "ANNUALLY";
  day_of_week: number | null;
  day_of_month: number | null;
  month_of_year: number | null;
  fund_to_limit: boolean;
  next_run_at: string;
  last_run_at: string | null;
  status: "ACTIVE" | "PAUSED" | "CANCELLED";
  failure_count: number;
  created_at: string;
}

/** What a schedule would look like if it filled the year's remaining IRA
 *  contribution room evenly across its remaining runs. */
export interface MaxFundingPlanT {
  tax_year: number;
  remaining: string;
  runs: number;
  per_run: string;
  final_run: string;
  total: string;
  first_run: string | null;
  last_run: string | null;
  catchup_included: boolean;
  eligible: boolean;
  notes: string[];
}

export interface AssetT {
  ticker: string;
  name: string;
  asset_class: string;
  expense_ratio: string | null;
  category: string;
  region: string;
  prospectus_url: string | null;
  auto_registered: boolean;
}

export interface QuoteT {
  ticker: string;
  price: string;
  prev_close: string | null;
  change_pct: number | null;
  as_of: string;
  provider: string;
}

export interface ExchangeLotT {
  acquired_on: string;
  shares: string;
  cost_per_share: string;
  proceeds: string;
  gain: string;
  term: "SHORT" | "LONG";
}

export interface ExchangePreviewT {
  account_id: string;
  account_type: AccountTypeT;
  taxable: boolean;
  from_ticker: string;
  to_ticker: string;
  price: string;
  shares: string;
  gross_proceeds: string;
  fees: string;
  net_proceeds: string;
  cost_basis: string;
  cost_basis_method: CostBasisMethodT;
  short_term_gains: string;
  long_term_gains: string;
  total_gains: string;
  estimated_shares_bought: string | null;
  lots: ExchangeLotT[];
  notes: string[];
}

export interface ExchangeResultT {
  sell: { order: OrderT; transaction: TransactionT | null };
  buy: { order: OrderT; transaction: TransactionT | null } | null;
  realized_gains: string | null;
  short_term_gains: string | null;
  long_term_gains: string | null;
  taxable: boolean;
  notes: string[];
}

export interface ScenarioT {
  id: string;
  name: string;
  description: string | null;
  sort_order: number;
  copied_from_id: string | null;
  account_count: number;
  is_default: boolean;
  is_active: boolean;
  created_at: string;
}

/** A scenario inside its retention window — still recoverable, with a clock. */
export interface DeletedScenarioT {
  id: string;
  name: string;
  description: string | null;
  account_count: number;
  deleted_at: string;
  purges_at: string;
  days_left: number;
  hours_left: number;
  retention_days: number;
}

export const MAX_SCENARIOS = 100;

export interface MeT {
  id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  full_name: string;
  date_of_birth: string;
  mfa_enabled: boolean;
  email_verified: boolean;
  default_range: RangeT;
  default_scenario_id: string | null;
  created_at: string;
}

export const ACCOUNT_TYPE_LABEL: Record<string, string> = {
  TAXABLE: "Taxable Brokerage",
  ROTH_IRA: "Roth IRA",
  TRADITIONAL_IRA: "Traditional IRA",
  ROLLOVER_IRA: "Rollover IRA",
};
