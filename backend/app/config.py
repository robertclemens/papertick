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

    market_data_provider: str = "auto"  # auto | polygon | alpaca | yahoo | synthetic
    polygon_api_key: str = ""
    alpaca_api_key_id: str = ""
    alpaca_api_secret: str = ""
    trade_fee_usd: Decimal = Decimal("0.00")
    slippage_bps: int = 2
    option_fee_per_contract: Decimal = Decimal("0.65")
    risk_free_rate: float = 0.045  # used by the option pricing model
    # Settlement fund (VMFXX) 7-day SEC yield, annualized. 0 = use the built-in
    # rate history in app/services/settlement.py.
    settlement_yield_annual: float = 0.0
    # Real-market emulation: queue market orders outside NYSE hours for the next
    # open, and price mutual funds at the daily closing NAV. Set false to fill
    # everything instantly at the latest price (sandbox mode).
    enforce_market_hours: bool = True
    # Past-dated ("as of") fills rewrite history: they change balances and
    # realized gains inside periods that already have statements, and they let
    # someone place a trade knowing the outcome. Off by default; when on, any
    # statement covering the backdated date is regenerated after the fill.
    allow_backdated_trades: bool = False

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

    @field_validator("market_data_provider")
    @classmethod
    def _valid_provider(cls, v: str) -> str:
        if v not in {"auto", "polygon", "alpaca", "yahoo", "synthetic"}:
            raise ValueError("MARKET_DATA_PROVIDER must be auto, polygon, alpaca, yahoo or synthetic")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
