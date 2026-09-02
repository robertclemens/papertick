import io
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import segno
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import ACCESS_COOKIE, REFRESH_COOKIE, Principal, get_principal, require_manage
from app.models import RefreshToken, User, utcnow
from app.rate_limit import (
    clear_login_failures,
    client_ip,
    enforce_account_limit,
    is_locked_out,
    rate_limiter,
    record_login_failure,
)
from app.schemas import (
    DeviceLoginIn,
    DobImpactOut,
    EmailTokenIn,
    LoginIn,
    LoginOut,
    MfaCodeIn,
    MfaDisableIn,
    MfaLoginIn,
    MfaSetupIn,
    PasswordChangeIn,
    ProfileUpdateIn,
    ProfileUpdateOut,
    ResendVerificationIn,
    SignupIn,
    TokenPair,
    TrustedDeviceOut,
    UserOut,
)
from app import security
from app.services import devices, scenarios
from app.services.mailer import send_email_change_email, send_verification_email

log = logging.getLogger("papertick.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Argon2 hash of a random throwaway password; verified against on unknown
# emails so response timing does not reveal whether an account exists.
_DUMMY_HASH = security.hash_password("t1m1ng-3qual1zer-Xq9!padding")

# One message for every credential failure, so responses never distinguish
# "no such account" from "wrong password" from "this source is locked out".
INVALID_LOGIN = "Invalid email or password"


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _cookie_paths() -> tuple[str, str]:
    """(access, refresh) cookie paths, sub-folder deployments included.

    A cookie path is matched against the URL the browser requested, which still
    carries BASE_PATH — the prefix is only stripped later, by the frontend's
    proxy. Without it here the refresh cookie would be scoped to a path that is
    never requested and would simply never be sent back.

    Scoping the access cookie to the app's own prefix rather than the whole
    domain is also the tighter choice: on a shared hostname the session is not
    handed to whatever else lives there.
    """
    base = get_settings().base_path
    return base or "/", f"{base}/api/v1/auth"


def _set_cookies(response: Response, access: str, refresh: str) -> None:
    s = get_settings()
    access_path, refresh_path = _cookie_paths()
    response.set_cookie(
        ACCESS_COOKIE, access,
        max_age=s.access_token_ttl_seconds, httponly=True,
        samesite="lax", secure=s.cookie_secure, path=access_path,
    )
    response.set_cookie(
        REFRESH_COOKIE, refresh,
        max_age=s.refresh_token_ttl_seconds, httponly=True,
        samesite="lax", secure=s.cookie_secure, path=refresh_path,
    )


def _clear_cookies(response: Response) -> None:
    access_path, refresh_path = _cookie_paths()
    response.delete_cookie(ACCESS_COOKIE, path=access_path)
    response.delete_cookie(REFRESH_COOKIE, path=refresh_path)


def _issue_tokens(db: Session, user: User, response: Response) -> TokenPair:
    s = get_settings()
    access = security.make_access_token(user.id)
    raw_refresh, refresh_hash = security.new_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=refresh_hash,
        expires_at=utcnow() + timedelta(seconds=s.refresh_token_ttl_seconds),
    ))
    db.commit()
    _set_cookies(response, access, raw_refresh)
    return TokenPair(access_token=access, refresh_token=raw_refresh, expires_in=s.access_token_ttl_seconds)


@router.post("/signup", response_model=LoginOut, status_code=201,
             dependencies=[Depends(rate_limiter("signup", 10, 3600))])
def signup(data: SignupIn, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    """Creates the account. Outside production it logs the user in immediately;
    in production the account starts unverified and a verification link is
    sent (or logged, with no SMTP configured) instead of tokens being issued."""
    try:
        security.validate_password_strength(data.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    email = data.email.lower()
    # Per-address as well as per-source: behind the bundled proxy the per-IP
    # bucket is shared by everyone, so this is what stops one caller from
    # spending the whole signup allowance.
    enforce_account_limit("signup", email, 5, 3600)
    production = get_settings().is_production
    exists = db.execute(select(User.id).where(User.email == email)).first()
    if exists:
        if production:
            # Confirming that an address is already registered is free account
            # enumeration. Outside production the explicit 409 is kept, since
            # developers hit this constantly and there is nothing to protect.
            log.info("signup attempted for an existing address")
            return LoginOut(verification_required=True)
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(
        email=email,
        password_hash=security.hash_password(data.password),
        first_name=(data.first_name or "").strip() or None,
        last_name=(data.last_name or "").strip() or None,
        date_of_birth=data.date_of_birth,
        email_verified=not production,
    )
    db.add(user)
    db.flush()
    # A user with no scenario has nowhere to put an account, and the startup
    # backfill only runs at boot — so create it here, in the same transaction.
    scenarios.ensure_default(db, user)
    db.commit()
    if production:
        send_verification_email(email, security.make_email_verify_token(user.id))
        return LoginOut(verification_required=True)
    return LoginOut(tokens=_issue_tokens(db, user, response))


@router.post("/login", response_model=LoginOut,
             dependencies=[Depends(rate_limiter("login", 20, 60))])
def login(data: LoginIn, request: Request, response: Response,
          db: Session = Depends(get_db)) -> LoginOut:
    """First step of the password login path: checks credentials against the
    lockout window and, in production, requires a verified email. If MFA is
    enrolled it returns an `mfa_token` for `/login/mfa` instead of session
    tokens; unknown emails are checked against a dummy hash so response
    timing doesn't reveal whether the account exists."""
    email = data.email.lower()
    enforce_account_limit("login", email, 30, 300)
    ip = client_ip(request)
    # A locked source gets the same 401 as a wrong password: a distinct status
    # would confirm the address belongs to a real account.
    if is_locked_out(email, ip):
        raise HTTPException(status_code=401, detail=INVALID_LOGIN)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        security.verify_password(data.password, _DUMMY_HASH)
        record_login_failure(email, ip)
        raise HTTPException(status_code=401, detail=INVALID_LOGIN)
    if not security.verify_password(data.password, user.password_hash) or not user.is_active:
        record_login_failure(email, ip)
        raise HTTPException(status_code=401, detail=INVALID_LOGIN)
    if get_settings().is_production and not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Check your inbox for the confirmation link, or request a new one.",
        )
    if user.passkey_only:
        # The password was correct, but this account has opted out of the
        # password path. Said plainly: the user turned this on deliberately and
        # needs to know why their password is being refused. It leaks nothing
        # a correct password did not already confirm.
        raise HTTPException(
            status_code=403,
            detail="This account signs in with a passkey. Use “Use a passkey” instead.",
        )
    clear_login_failures(email, ip)
    if security.password_needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(data.password)
        db.commit()
    if user.mfa_enabled:
        return LoginOut(mfa_required=True, mfa_token=security.make_mfa_token(user.id))
    if devices.verification_required(db, user, request):
        return LoginOut(
            device_verification_required=True,
            device_token=devices.start_challenge(db, user, request),
        )
    tokens = _issue_tokens(db, user, response)
    _remember_if_applicable(db, user, request, response)
    return LoginOut(tokens=tokens)


@router.post("/login/mfa", response_model=LoginOut,
             dependencies=[Depends(rate_limiter("mfa", 15, 60))])
def login_mfa(data: MfaLoginIn, request: Request, response: Response,
              db: Session = Depends(get_db)) -> LoginOut:
    """Completes the password login path using the `mfa_token` returned by
    `/login`: verifies the TOTP code and, on success, issues session tokens.
    The token is short-lived and only valid for this step."""
    payload = security.decode_token(data.mfa_token, "mfa")
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token, log in again")
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active or not user.mfa_enabled or not user.mfa_secret_enc:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA token, log in again")
    ip = client_ip(request)
    if is_locked_out(user.email, ip):
        raise HTTPException(status_code=401, detail="Invalid authentication code")
    secret = security.open_totp_secret(user.mfa_secret_enc)
    if secret is None or not security.verify_totp(secret, data.code):
        record_login_failure(user.email, ip)
        raise HTTPException(status_code=401, detail="Invalid authentication code")
    clear_login_failures(user.email, ip)
    return LoginOut(tokens=_issue_tokens(db, user, response))


def _remember_if_applicable(db: Session, user: User, request: Request,
                            response: Response) -> None:
    """Leave a device token behind when device verification is the account's
    fallback factor and this browser has none yet. Accounts with a passkey or
    an authenticator never take this path, so no cookie is minted for them."""
    s = get_settings()
    if not (s.device_verification and s.is_production):
        return
    if devices.has_second_factor(db, user):
        return
    if devices.is_trusted(db, user, request.cookies.get(devices.DEVICE_COOKIE)):
        return
    devices.remember(db, user, request, response)


@router.post("/login/device", response_model=LoginOut,
             dependencies=[Depends(rate_limiter("device-otp", 15, 300))])
def login_device(data: DeviceLoginIn, request: Request, response: Response,
                 db: Session = Depends(get_db)) -> LoginOut:
    """Completes a sign-in from an unrecognised browser using the one-time code
    emailed by `/login`. On success the browser is remembered for
    DEVICE_TRUST_DAYS and skips this step next time."""
    user_id = devices.verify_challenge(data.device_token, data.code)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired code")
    tokens = _issue_tokens(db, user, response)
    devices.remember(db, user, request, response)
    return LoginOut(tokens=tokens)


@router.get("/devices", response_model=list[TrustedDeviceOut])
def list_devices(principal: Principal = Depends(require_manage),
                 db: Session = Depends(get_db)):
    """Browsers this account has verified and that may skip the new-device
    code, newest first."""
    return [TrustedDeviceOut.model_validate(d) for d in devices.list_for(db, principal.user)]


@router.delete("/devices/{device_id}", status_code=204)
def revoke_device(device_id: str, principal: Principal = Depends(require_manage),
                  db: Session = Depends(get_db)) -> None:
    """Forget one browser: its next sign-in needs a fresh code."""
    if not devices.revoke(db, principal.user, device_id):
        raise HTTPException(status_code=404, detail="Device not found")


@router.delete("/devices", status_code=204)
def revoke_all_devices(response: Response, principal: Principal = Depends(require_manage),
                       db: Session = Depends(get_db)) -> None:
    """Forget every remembered browser, this one included."""
    devices.revoke_all(db, principal.user)
    devices.forget_cookie(response)


@router.post("/refresh", response_model=LoginOut,
             dependencies=[Depends(rate_limiter("refresh", 30, 60))])
def refresh(request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    """Rotates the refresh token: the presented one is marked revoked and a
    new access/refresh pair is issued and set as cookies. Presenting a token
    that was already rotated is treated as theft and revokes every active
    session for the user."""
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token")
    token = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_refresh_token(raw))
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    now = datetime.now(timezone.utc)
    expires_at = token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=timezone.utc)
    if token.revoked_at is not None:
        # Reuse of a rotated token: assume theft, revoke the whole family.
        db.query(RefreshToken).filter(
            RefreshToken.user_id == token.user_id, RefreshToken.revoked_at.is_(None)
        ).update({"revoked_at": now})
        db.commit()
        _clear_cookies(response)
        raise HTTPException(status_code=401, detail="Refresh token reuse detected; all sessions revoked")
    if expires_at <= now:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    token.revoked_at = now
    pair = _issue_tokens(db, user, response)
    token.replaced_by = security.hash_refresh_token(pair.refresh_token)[:36]
    db.commit()
    return LoginOut(tokens=pair)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
    """Revokes the current session's refresh token and access token and clears
    the auth cookies; other active sessions for the user are left untouched."""
    access = request.cookies.get(ACCESS_COOKIE) or _bearer(request)
    if access:
        security.revoke_access_token(access)
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        db.query(RefreshToken).filter(
            RefreshToken.token_hash == security.hash_refresh_token(raw)
        ).update({"revoked_at": utcnow()})
        db.commit()
    _clear_cookies(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(principal: Principal = Depends(get_principal)) -> UserOut:
    """Returns the authenticated user's profile, as resolved from the
    request's access token."""
    return UserOut.model_validate(principal.user)


# ------------------------------------------------------------------ email verification

@router.post("/verify-email", dependencies=[Depends(rate_limiter("verify-email", 20, 3600))])
def verify_email(data: EmailTokenIn, db: Session = Depends(get_db)) -> dict:
    """Handles both signup verification and email-change confirmation links."""
    payload = security.decode_token(data.token, "email_verify")
    if payload is not None:
        user = db.get(User, payload["sub"])
        if user is None:
            raise HTTPException(status_code=422, detail="Invalid verification link")
        user.email_verified = True
        db.commit()
        return {"status": "verified", "email": user.email}

    payload = security.decode_token(data.token, "email_change")
    if payload is not None:
        user = db.get(User, payload["sub"])
        new_email = (payload.get("new_email") or "").lower()
        if user is None or not new_email:
            raise HTTPException(status_code=422, detail="Invalid verification link")
        taken = db.execute(
            select(User.id).where(User.email == new_email, User.id != user.id)
        ).first()
        if taken:
            raise HTTPException(status_code=409, detail="That email address is already in use")
        user.email = new_email
        user.email_verified = True
        db.commit()
        return {"status": "email_changed", "email": new_email}

    raise HTTPException(status_code=422, detail="Invalid or expired verification link")


@router.post("/resend-verification", status_code=202,
             dependencies=[Depends(rate_limiter("resend-verify", 5, 3600))])
def resend_verification(data: ResendVerificationIn, db: Session = Depends(get_db)) -> dict:
    """Re-sends the signup verification link if the address belongs to an
    unverified account. The response is identical either way, so it can't be
    used to probe whether an account exists."""
    user = db.execute(select(User).where(User.email == data.email.lower())).scalar_one_or_none()
    if user is not None and not user.email_verified:
        send_verification_email(user.email, security.make_email_verify_token(user.id))
    # identical response either way — no account enumeration
    return {"status": "sent_if_pending"}


# ------------------------------------------------------------------ profile

def _dob_impact_warnings(db: Session, user: User, new_dob) -> list[str]:
    from app.models import IRA_TYPES, Account, AccountType, CashFlowKind, Contribution, IrsLimit
    from sqlalchemy import func

    ira_like = IRA_TYPES | {AccountType.ROLLOVER_IRA}
    rows = db.execute(
        select(Contribution.tax_year, func.sum(Contribution.amount))
        .join(Account, Account.id == Contribution.account_id)
        .where(
            Account.user_id == user.id,
            Account.account_type.in_(ira_like),
            Contribution.kind == CashFlowKind.CONTRIBUTION,
            Contribution.tax_year.isnot(None),
        )
        .group_by(Contribution.tax_year)
    ).all()
    contributed = {year: Decimal(total) for year, total in rows}
    years = sorted(set(contributed) | {date.today().year})
    warnings: list[str] = []
    for year in years:
        limit_row = db.get(IrsLimit, year)
        if limit_row is None:
            continue
        old_catchup = year - user.date_of_birth.year >= limit_row.catchup_age
        new_catchup = year - new_dob.year >= limit_row.catchup_age
        if old_catchup == new_catchup:
            continue
        new_limit = Decimal(limit_row.ira_limit) + (
            Decimal(limit_row.ira_catchup) if new_catchup else Decimal(0)
        )
        amt = contributed.get(year, Decimal(0))
        if amt > new_limit:
            warnings.append(
                f"Tax year {year}: you would lose catch-up eligibility and your "
                f"${amt} of contributions would EXCEED the ${new_limit} limit — "
                "an over-contribution in the historical record."
            )
        else:
            direction = "gain" if new_catchup else "lose"
            warnings.append(
                f"Tax year {year}: you would {direction} catch-up eligibility "
                f"(limit becomes ${new_limit}; ${amt} contributed)."
            )
    return warnings


@router.post("/password", response_model=UserOut,
             dependencies=[Depends(rate_limiter("password", 5, 3600))])
def change_password(data: PasswordChangeIn, response: Response,
                    principal: Principal = Depends(require_manage),
                    db: Session = Depends(get_db)) -> UserOut:
    """Change the sign-in password. Every other session is signed out (a
    password change is the standard way to evict a session you don't control);
    this one is re-issued fresh cookies so the user stays put."""
    user = principal.user
    if not security.verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if data.new_password == data.current_password:
        raise HTTPException(status_code=422, detail="The new password must be different")
    try:
        security.validate_password_strength(data.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    user.password_hash = security.hash_password(data.new_password)
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
    ).update({"revoked_at": utcnow()})
    db.commit()
    # Refresh tokens alone are not enough: every access token already issued
    # stays valid for its full lifetime, so a password change would not
    # actually evict a session the user does not control.
    security.revoke_all_access_tokens(user.id)
    _issue_tokens(db, user, response)
    log.info("password changed for %s", user.id)
    return UserOut.model_validate(user)


@router.get("/profile/dob-impact", response_model=DobImpactOut)
def dob_impact(date_of_birth: date, principal: Principal = Depends(require_manage),
               db: Session = Depends(get_db)) -> DobImpactOut:
    """Previews what changing the stored date of birth would do to the
    user's existing IRA contribution history: for each tax year on record it
    checks whether the change crosses the catch-up-eligibility age threshold,
    and flags any year where the contributions already made would become an
    over-contribution under the resulting limit. Read-only — `/profile` is
    what actually applies the change."""
    return DobImpactOut(warnings=_dob_impact_warnings(db, principal.user, date_of_birth))


@router.patch("/profile", response_model=ProfileUpdateOut,
              dependencies=[Depends(rate_limiter("profile", 10, 3600))])
def update_profile(data: ProfileUpdateIn, principal: Principal = Depends(require_manage),
                   db: Session = Depends(get_db)) -> ProfileUpdateOut:
    """Applies profile field updates. A date-of-birth change that would
    affect contribution-limit eligibility is rejected with a 409 (listing the
    warnings) unless `confirm_impacts` is set. An email change requires the
    current password and, in production, doesn't take effect until confirmed
    via the emailed link; in development it's applied immediately."""
    user = principal.user
    email_change = "none"
    warnings: list[str] = []

    if data.first_name is not None:
        user.first_name = data.first_name.strip() or None
    if data.last_name is not None:
        user.last_name = data.last_name.strip() or None
    if data.default_range is not None:
        user.default_range = data.default_range
    if data.default_scenario_id is not None:
        from app.services.scenarios import owned as _owned_scenario

        _owned_scenario(db, user, data.default_scenario_id)   # 404s if not theirs
        user.default_scenario_id = data.default_scenario_id

    if data.date_of_birth is not None and data.date_of_birth != user.date_of_birth:
        impacts = _dob_impact_warnings(db, user, data.date_of_birth)
        if impacts and not data.confirm_impacts:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Changing your birthdate affects contribution limits — confirm to proceed",
                    "warnings": impacts,
                },
            )
        user.date_of_birth = data.date_of_birth
        warnings = impacts

    if data.email is not None and data.email.lower() != user.email:
        new_email = data.email.lower()
        if not data.current_password or not security.verify_password(
            data.current_password, user.password_hash
        ):
            raise HTTPException(status_code=401, detail="Current password required to change email")
        taken = db.execute(
            select(User.id).where(User.email == new_email, User.id != user.id)
        ).first()
        if taken:
            raise HTTPException(status_code=409, detail="That email address is already in use")
        if get_settings().is_production:
            send_email_change_email(new_email, security.make_email_change_token(user.id, new_email))
            email_change = "verification_sent"
        else:
            user.email = new_email
            email_change = "applied"

    db.commit()
    return ProfileUpdateOut(user=UserOut.model_validate(user), email_change=email_change, warnings=warnings)


# ------------------------------------------------------------------ MFA

@router.post("/mfa/setup", dependencies=[Depends(rate_limiter("mfa-setup", 10, 3600))])
def mfa_setup(data: MfaSetupIn, principal: Principal = Depends(require_manage),
              db: Session = Depends(get_db)) -> dict:
    """Generates a new TOTP secret and stores it encrypted on the user, but
    does not turn MFA on yet — `/mfa/enable` with a valid code does that.
    Returns the raw secret, its otpauth URI, and a QR code SVG for
    authenticator apps to scan.

    Requires the current password: this response *is* the second factor, so
    handing it out on a session cookie alone would let a hijacked session mint
    itself one. Disabling MFA already asks for the password; enrolling should
    match."""
    user = principal.user
    if not security.verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="MFA is already enabled")
    secret = security.new_totp_secret()
    user.mfa_secret_enc = security.seal_totp_secret(secret)
    db.commit()
    uri = security.totp_provisioning_uri(secret, user.email)
    buf = io.BytesIO()
    segno.make(uri).save(buf, kind="svg", scale=4, dark="#0f172a", light=None)
    return {"otpauth_uri": uri, "secret": secret, "qr_svg": buf.getvalue().decode("utf-8")}


@router.post("/mfa/enable")
def mfa_enable(data: MfaCodeIn, principal: Principal = Depends(require_manage),
               db: Session = Depends(get_db)) -> dict:
    """Verifies the code against the secret generated by `/mfa/setup` and, on
    success, turns MFA on for the account."""
    user = principal.user
    if not user.mfa_secret_enc:
        raise HTTPException(status_code=409, detail="Run MFA setup first")
    secret = security.open_totp_secret(user.mfa_secret_enc)
    if secret is None or not security.verify_totp(secret, data.code):
        raise HTTPException(status_code=422, detail="Invalid authentication code")
    user.mfa_enabled = True
    db.commit()
    return {"mfa_enabled": True}


@router.post("/mfa/disable")
def mfa_disable(data: MfaDisableIn, principal: Principal = Depends(require_manage),
                db: Session = Depends(get_db)) -> dict:
    """Turns MFA off, requiring both the current password and a valid TOTP
    code as step-up confirmation, and discards the stored secret so a fresh
    `/mfa/setup` would be needed to re-enable it."""
    user = principal.user
    if not user.mfa_enabled or not user.mfa_secret_enc:
        raise HTTPException(status_code=409, detail="MFA is not enabled")
    if not security.verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid password")
    secret = security.open_totp_secret(user.mfa_secret_enc)
    if secret is None or not security.verify_totp(secret, data.code):
        raise HTTPException(status_code=422, detail="Invalid authentication code")
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    db.commit()
    return {"mfa_enabled": False}
