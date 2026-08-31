"""Authentication dependencies: JWT (cookie or Bearer) and scoped API keys.

Every request resolves to a Principal carrying the user and granted scopes:
  - Session JWT  -> scopes {read, trade, manage}  (full interactive access)
  - API key      -> scopes stored on the key      (read and/or trade)
"""

from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Account, ApiKey, Scenario, User, utcnow
from app.security import (
    API_KEY_PREFIX,
    access_token_revoked,
    decode_token,
    hash_api_key,
)

def _cookie_names() -> tuple[str, str]:
    """Cookie names, prefixed when the deployment can honour the prefix rules.

    `__Host-` pins a cookie to the exact origin: it requires Secure, forbids a
    Domain attribute and forces Path=/, which closes the sibling-subdomain hole
    SameSite leaves open (SameSite is scoped to the registrable domain, not the
    origin). The refresh cookie keeps its narrower Path=/api/v1/auth, which
    `__Host-` forbids, so it takes `__Secure-` instead. Over plain HTTP no
    prefix is usable and the bare names are kept.
    """
    from app.config import get_settings

    if get_settings().cookie_secure:
        return "__Host-pt_access", "__Secure-pt_refresh"
    return "pt_access", "pt_refresh"


ACCESS_COOKIE, REFRESH_COOKIE = _cookie_names()

SESSION_SCOPES = {"read", "trade", "manage"}
VALID_KEY_SCOPES = {"read", "trade"}


SCENARIO_HEADER = "X-Scenario-Id"


@dataclass
class Principal:
    user: User
    scopes: set[str] = field(default_factory=set)
    via_api_key: bool = False
    # the scenario this request reads and writes; every account query is
    # filtered by it, so a scenario is a complete, isolated track of data
    scenario: Scenario | None = None

    @property
    def scenario_id(self) -> str | None:
        return self.scenario.id if self.scenario else None


def resolve_scenario(request: Request, user: User, db: Session,
                     header_value: str | None = None) -> Scenario | None:
    """Which scenario the caller is working in.

    An explicit `X-Scenario-Id` header (or `scenario_id` query parameter, for
    links and one-off calls that cannot set headers) wins; otherwise the user's
    chosen default; otherwise their first scenario. A caller may only name a
    scenario they own — anything else is a 404, never a silent fallback to the
    default, so a wrong id can never quietly read or write the wrong track.
    """
    requested = (
        header_value
        or request.headers.get(SCENARIO_HEADER)
        or request.query_params.get("scenario_id")
    )
    if requested:
        scenario = db.get(Scenario, requested)
        if scenario is None or scenario.user_id != user.id:
            raise HTTPException(status_code=404, detail="Scenario not found")
        if scenario.deleted_at is not None:
            # a deleted scenario is frozen; working in it would resurrect data
            # the user asked to throw away
            raise HTTPException(status_code=404, detail="Scenario has been deleted")
        return scenario
    if user.default_scenario_id:
        scenario = db.get(Scenario, user.default_scenario_id)
        if scenario is not None and scenario.user_id == user.id \
                and scenario.deleted_at is None:
            return scenario
    return db.execute(
        select(Scenario).where(Scenario.user_id == user.id, Scenario.deleted_at.is_(None))
        .order_by(Scenario.sort_order, Scenario.created_at)
    ).scalars().first()


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


SCENARIO_DOC = (
    "Scenario to read and write. Scenarios are independent tracks of accounts, "
    "holdings and history: every account-scoped call is filtered by this. "
    "Omit it to use your default scenario (GET /scenarios lists them, and the "
    "response header X-Scenario-Id reports which one answered). A "
    "`scenario_id` query parameter works too, for callers that cannot set headers."
)


def get_principal(
    request: Request,
    db: Session = Depends(get_db),
    x_scenario_id: str | None = Header(default=None, alias=SCENARIO_HEADER,
                                       description=SCENARIO_DOC),
) -> Principal:
    token = _bearer_token(request)

    # API key path
    if token and token.startswith(API_KEY_PREFIX):
        key = db.query(ApiKey).filter(ApiKey.key_hash == hash_api_key(token)).first()
        if key is None or key.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Invalid API key")
        user = db.get(User, key.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid API key")
        key.last_used_at = utcnow()
        db.commit()
        scopes = {s for s in key.scopes.split(",") if s in VALID_KEY_SCOPES}
        principal = Principal(user=user, scopes=scopes, via_api_key=True,
                              scenario=resolve_scenario(request, user, db, x_scenario_id))
        request.state.scenario = principal.scenario
        return principal

    # Session JWT path (Authorization header or httpOnly cookie)
    if token is None:
        token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token, "access")
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Signing out and changing a password revoke the session's refresh token;
    # without this the stateless access token stays usable until it expires.
    if access_token_revoked(payload):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    principal = Principal(user=user, scopes=set(SESSION_SCOPES),
                          scenario=resolve_scenario(request, user, db, x_scenario_id))
    request.state.scenario = principal.scenario
    return principal


def require_scope(scope: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if scope not in principal.scopes:
            raise HTTPException(status_code=403, detail=f"Missing required scope: {scope}")
        return principal

    return dependency


require_read = require_scope("read")
require_trade = require_scope("trade")
require_manage = require_scope("manage")  # session-only: keys, MFA, account admin


def owned_account(account_id: str, principal: Principal, db: Session) -> Account:
    """An account the caller owns *in the active scenario*. Naming an account
    from another scenario reads as not-found, so a stale id from a scenario
    switch can never write into the wrong track."""
    account = db.get(Account, account_id)
    if account is None or account.user_id != principal.user.id:
        raise HTTPException(status_code=404, detail="Account not found")
    if principal.scenario is not None and account.scenario_id != principal.scenario.id:
        raise HTTPException(status_code=404, detail="Account not found in this scenario")
    return account
