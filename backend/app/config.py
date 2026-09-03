from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings

WEAK_SECRETS = {"", "changeme", "secret", "CHANGE_ME_openssl_rand_hex_48"}


class Settings(BaseSettings):
    env: str = "development"
    database_url: str = "postgresql+psycopg://papertick:papertick@localhost:5432/papertick"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str

    frontend_origin: str = "http://localhost:3000"
    cookie_secure: bool = False

    # Sub-folder the UI is served under behind a reverse proxy ("" at a domain
    # root, "/papertick" for https://domain.example/papertick/). Only affects
    # links generated back into the app: FRONTEND_ORIGIN stays a bare origin
    # because CORS, the cross-origin guard and WebAuthn all compare against the
    # browser's Origin header, which never carries a path.
    base_path: str = ""

    # Comma-separated CIDRs of reverse proxies whose X-Forwarded-For may be
    # believed. Empty (the default) means trust nothing: the peer address is
    # used, so a client cannot pick its own rate-limit bucket. Set this to the
    # ingress/load-balancer range in any deployment that sits behind one.
    trusted_proxy_cidrs: str = ""

    # Comma-separated Host values this deployment answers to. Empty disables the
    # check (fine for local development); set it in production so a spoofed Host
    # cannot reach routing or a generated URL.
    allowed_hosts: str = ""

    access_token_ttl_seconds: int = 60 * 15
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 7
    mfa_token_ttl_seconds: int = 60 * 5

    market_data_provider: str = "auto"  # auto | polygon | tiingo | alpaca | yahoo | nasdaq | synthetic
    # Free, keyless backstop for a Yahoo outage. EQUITIES AND ETFs ONLY — its
    # mutual-fund series is unadjusted for splits, so funds are never requested
    # from it and simply fall through. Sits last in the chain: it serves daily
    # closes rather than live prints, so it is a fallback, not a peer.
    nasdaq_fallback: bool = True
    polygon_api_key: str = ""
    tiingo_api_token: str = ""
    alpaca_api_key_id: str = ""
    alpaca_api_secret: str = ""

    # --- IRS contribution-limit refresh -------------------------------------
    # Reads the published IRA limits back from irs.gov and reconciles them
    # against the seeded table, so a year carried forward as a projection is
    # replaced by official figures once the IRS publishes them (usually in the
    # October/November COLA release). Turn it off for an air-gapped or offline
    # deployment: the app still works, it just keeps its projections until
    # someone updates the seed table by hand.
    irs_limit_refresh: bool = True
    # Restricts the source chain to the named sources, in order
    # ("cola", "pub590a"). Empty uses the full chain.
    irs_limit_sources: str = ""

    # --- Price-convention verification --------------------------------------
    # Historical prices must be split-adjusted and NOT dividend-adjusted, or
    # dividends are counted twice (see services/convention.py). A vendor can
    # change that under us silently, so each provider is measured against
    # fixtures whose answers are permanent historical fact.
    convention_probe: bool = True
    # How stale a verdict may be at the moment a price is about to be written
    # into the ledger. Enforcement is per-fill and effectively free (a cached
    # verdict is a 0.13ms Redis read); only re-measuring costs anything
    # (~800ms, two requests), and this is what amortises it. Lower is safer
    # and costs more: 24h bounds the exposure to a vendor changing convention
    # at two requests a day, and only on days something actually trades.
    # 0 re-measures before every fill.
    convention_max_age_hours: int = 24
    # Drop a provider proven to be on the wrong convention. On by default:
    # wrong prices become permanent ledger rows, whereas a missing provider
    # only holds orders until it returns. Only a confident mismatch
    # quarantines — a failed fetch never does.
    convention_quarantine: bool = True
    trade_fee_usd: Decimal = Decimal("0.00")

    # --- Simulated slippage -------------------------------------------------
    # `fixed` applies SLIPPAGE_BPS adversely to every live market fill (the
    # original behaviour). `variable` draws each fill from a triangular
    # distribution over [MIN, MAX] with its mode at SLIPPAGE_BPS, which is what
    # a real fill distribution looks like: clustered near the typical spread,
    # a long adverse tail, and an occasional negative draw for the price
    # improvement retail flow genuinely receives. The draw is seeded from the
    # order id, so replaying a backtest reproduces the same fills.
    slippage_model: str = "variable"     # variable | fixed
    slippage_bps: int = 2                # fixed: the value. variable: the mode.
    slippage_bps_min: int = -1           # negative = price improvement
    slippage_bps_max: int = 6
    option_fee_per_contract: Decimal = Decimal("0.65")
    risk_free_rate: float = 0.045  # used by the option pricing model
    # Settlement fund (VMFXX) 7-day SEC yield, annualized. 0 = use the built-in
    # rate history in app/services/settlement.py.
    settlement_yield_annual: float = 0.0
    # Real-market emulation: queue market orders outside NYSE hours for the next
    # open, and price mutual funds at the daily closing NAV. Set false to fill
    # everything instantly at the latest price (sandbox mode).
    enforce_market_hours: bool = True
    # --- Market data polling & upstream budget ------------------------------
    # Cache lifetimes, in seconds, for what the providers return. Quotes are
    # the hot path: every page load that shows a price reads through this, so
    # it is the main thing standing between a busy dashboard and a rate-limit
    # ban from an unofficial endpoint. Floors are enforced in the validator.
    quote_cache_seconds: int = 30
    history_cache_seconds: int = 3600
    dividend_cache_seconds: int = 90000   # > 24h: outlives the daily reconcile sweep
    # Ceiling on outbound provider calls per minute across every process
    # (api + worker + beat share one Redis token bucket). 0 disables the
    # budget. When exhausted, callers fall back to the cached value if there
    # is one and raise MarketDataError if there is not, rather than queueing.
    market_upstream_per_minute: int = 120
    # How often the web UI re-prices an open page *while the market is open*.
    # Outside trading hours the server tells the client not to poll at all,
    # because nothing it could learn has changed (see
    # market_calendar.refresh_cadence). 0 disables auto-refresh entirely.
    #
    # This is not multiplied by the number of viewers: quotes are served from
    # one shared server-side cache, so the upstream ceiling is
    # (tickers held / QUOTE_CACHE_SECONDS) regardless of how many people are
    # watching.
    market_refresh_seconds: int = 60
    # Shortest gap between forced NAV re-fetches for one fund/day while an
    # order waits for its close to publish, and how long to keep trying.
    nav_poll_interval_seconds: int = 300
    nav_poll_give_up_hours: int = 30
    # When our own providers have failed to price a fund order for the whole
    # give-up window, an independent source is asked whether that day's NAV
    # exists at all. If it does, the failure is ours, not the fund's, and
    # rejecting the user's order would be the wrong call — so it keeps being
    # held, up to this many days past the fill time. 0 disables the check and
    # restores the plain reject.
    nav_hold_max_days: int = 5
    # How long a due order keeps retrying while the real providers are
    # unreachable, before it is rejected. Synthetic prices are never
    # substituted, so an outage holds the order rather than filling it at an
    # invented number; this bounds how long it is held.
    market_data_give_up_hours: int = 24

    # --- Recurring-investment catch-up --------------------------------------
    # After an outage, replay every run a rule missed at that day's actual
    # close rather than collapsing them into one fill at today's price.
    catchup_missed_runs: bool = True
    # Ceiling on how far back a single catch-up will reach, so a long outage
    # (or a rule restored from an old backup) cannot replay years of trades.
    max_catchup_days: int = 30

    # --- New-device verification --------------------------------------------
    # Production only, and only for accounts with neither a passkey nor an
    # authenticator: an unrecognised browser must confirm a one-time code
    # emailed to the account before it gets a session. Recognised devices are
    # remembered for DEVICE_TRUST_DAYS.
    device_verification: bool = True
    device_trust_days: int = 30
    device_otp_ttl_seconds: int = 600
    # Password-reset links. Short by design: the link is a bearer credential
    # that can take over the account, and a reset is acted on within minutes.
    password_reset_ttl_seconds: int = 1800

    # How long a deleted scenario is recoverable before it is wiped for good.
    scenario_retention_days: int = 30

    demo_mode: bool = False
    demo_email: str = ""
    demo_password: str = ""

    login_max_failures: int = 10
    login_lockout_seconds: int = 900

    # outbound email (verification links). Unset SMTP_HOST -> links are logged
    # to the backend log instead of sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "PaperTick <no-reply@papertick.example>"
    smtp_starttls: bool = True
    email_token_ttl_seconds: int = 60 * 60 * 24

    # WebAuthn / passkeys. rp_id defaults to the FRONTEND_ORIGIN hostname.
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "PaperTick"

    @field_validator("base_path")
    @classmethod
    def _normalize_base_path(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.startswith("/"):
            v = "/" + v
        return v

    @property
    def app_url(self) -> str:
        """Public URL of the web UI, sub-folder included."""
        return self.frontend_origin.rstrip("/") + self.base_path

    @property
    def rp_id(self) -> str:
        if self.webauthn_rp_id:
            return self.webauthn_rp_id
        from urllib.parse import urlparse

        return urlparse(self.frontend_origin).hostname or "localhost"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @field_validator("secret_key")
    @classmethod
    def _strong_secret(cls, v: str) -> str:
        if v in WEAK_SECRETS or len(v) < 32:
            raise ValueError(
                "SECRET_KEY must be a random string of at least 32 chars "
                "(generate with: openssl rand -hex 48)"
            )
        return v

    @field_validator("slippage_model")
    @classmethod
    def _valid_slippage_model(cls, v: str) -> str:
        if v not in {"fixed", "variable"}:
            raise ValueError("SLIPPAGE_MODEL must be fixed or variable")
        return v

    @field_validator("quote_cache_seconds")
    @classmethod
    def _quote_floor(cls, v: int) -> int:
        # Below this a single active dashboard can outpace an unofficial
        # endpoint's tolerance on its own, so the floor is not negotiable.
        if v < 5:
            raise ValueError("QUOTE_CACHE_SECONDS must be at least 5")
        return v

    @field_validator("market_refresh_seconds")
    @classmethod
    def _refresh_floor(cls, v: int) -> int:
        if v and v < 15:
            raise ValueError("MARKET_REFRESH_SECONDS must be 0 (off) or at least 15")
        return v

    @field_validator("market_data_provider")
    @classmethod
    def _valid_provider(cls, v: str) -> str:
        if v not in {"auto", "polygon", "tiingo", "alpaca", "yahoo", "nasdaq", "synthetic"}:
            raise ValueError("MARKET_DATA_PROVIDER must be auto, polygon, tiingo, "
                             "alpaca, yahoo, nasdaq or synthetic")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
