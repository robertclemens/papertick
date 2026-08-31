from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, require_manage
from app.models import WebAuthnCredential
from app.rate_limit import rate_limiter
from app.schemas import LoginOut, PasskeyLoginVerifyIn, PasskeyOut, PasskeyRegisterVerifyIn
from app.services import passkeys

router = APIRouter(prefix="/auth/passkeys", tags=["passkeys"])


@router.post("/register/options")
def register_options(principal: Principal = Depends(require_manage),
                     db: Session = Depends(get_db)) -> dict:
    """Generates WebAuthn registration options for the signed-in user,
    excluding their existing passkeys so the same authenticator can't be
    re-registered. The challenge is stashed in Redis for 5 minutes and must
    be redeemed by `/register/verify`."""
    return passkeys.registration_options(db, principal.user)


@router.post("/register/verify", response_model=PasskeyOut, status_code=201)
def register_verify(data: PasskeyRegisterVerifyIn,
                    principal: Principal = Depends(require_manage),
                    db: Session = Depends(get_db)) -> PasskeyOut:
    """Completes the registration ceremony: verifies the browser's response
    against the challenge from `/register/options` and, on success, saves the
    new passkey to the account."""
    row = passkeys.verify_registration(db, principal.user, data.credential, data.nickname)
    return PasskeyOut.model_validate(row)


@router.get("", response_model=list[PasskeyOut])
def list_passkeys(principal: Principal = Depends(require_manage),
                  db: Session = Depends(get_db)):
    """Lists the signed-in user's registered passkeys, oldest first."""
    rows = db.execute(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == principal.user.id)
        .order_by(WebAuthnCredential.created_at)
    ).scalars().all()
    return [PasskeyOut.model_validate(r) for r in rows]


@router.delete("/{passkey_id}", status_code=204)
def delete_passkey(passkey_id: str, principal: Principal = Depends(require_manage),
                   db: Session = Depends(get_db)) -> None:
    """Deletes a passkey. Returns 404 for an id that doesn't exist or belongs
    to another user, rather than distinguishing the two."""
    row = db.get(WebAuthnCredential, passkey_id)
    if row is None or row.user_id != principal.user.id:
        raise HTTPException(status_code=404, detail="Passkey not found")
    db.delete(row)
    db.commit()


@router.post("/login/options", dependencies=[Depends(rate_limiter("pk-login", 30, 60))])
def login_options() -> dict:
    """First step of passkey login: issues a usernameless WebAuthn challenge,
    since passkeys are discoverable credentials the browser can offer without
    knowing the account first — there's deliberately no lookup by email here,
    as that would let a caller enumerate accounts. Returns a `flow_id` that
    scopes the challenge and must be passed to `/login/verify`."""
    flow_id, options = passkeys.authentication_options()
    return {"flow_id": flow_id, "options": options}


@router.post("/login/verify", response_model=LoginOut,
             dependencies=[Depends(rate_limiter("pk-verify", 30, 60))])
def login_verify(data: PasskeyLoginVerifyIn, response: Response,
                 db: Session = Depends(get_db)) -> LoginOut:
    """Completes passkey login: verifies the signed challenge against the
    stored public key and, on success, issues session tokens directly. There
    is no separate MFA step here, since the passkey ceremony already proves
    possession and (typically) device biometric/PIN verification."""
    from app.routers.auth import _issue_tokens

    user = passkeys.verify_authentication(db, data.flow_id, data.credential)
    return LoginOut(tokens=_issue_tokens(db, user, response))
