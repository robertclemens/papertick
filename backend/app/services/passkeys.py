"""WebAuthn passkey support (registration + passwordless authentication).

Uses py_webauthn for the full ceremony verification. Passkeys are registered as
discoverable credentials (resident keys) with user verification *required*, so
signing in needs no username and genuinely carries its own second factor
(possession + device PIN/biometric) rather than possession alone. Challenges
live in Redis for 5 minutes.

Both ceremonies send WebAuthn `hints` so a platform or third-party credential
manager is what the client offers, rather than a hardware security key — see
CREDENTIAL_HINTS.

A passkey is still one credential, not two: when the account also has TOTP
enrolled, the router asks for the code after the ceremony (see
routers/passkeys_router.py).
"""

import base64
import json
import secrets

import redis
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialHint,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config import get_settings
from app.models import User, WebAuthnCredential, utcnow
from app.rate_limit import get_redis

CHALLENGE_TTL = 300

#: WebAuthn L3 `hints`, in preference order, for both ceremonies.
#:
#: This is what makes a third-party password manager the offer on iOS. With no
#: hint, Safari cannot tell a passkey request from a hardware-security-key
#: request, and its sheet leads with "Security Key" — a physical key over NFC
#: or Lightning — so a user whose passkeys live in Bitwarden or 1Password is
#: prompted for hardware they do not have and never sees their manager.
#:
#: `client-device` means "a credential from this device", which on iOS covers
#: iCloud Keychain *and* every enabled AutoFill provider — that is the entry
#: Bitwarden supplies. `hybrid` keeps the QR/nearby-device path for signing in
#: on a machine whose passkeys live on a phone. `security-key` is deliberately
#: absent: nothing is blocked by leaving it out (a client that has only a
#: security key still offers it), it simply stops being the headline.
#:
#: Deliberately expressed as hints rather than
#: `authenticatorSelection.authenticatorAttachment`. Pinning attachment to
#: `platform` would get the same iOS sheet at the cost of breaking hybrid
#: sign-in and hardware keys outright; hints steer the picker without removing
#: anyone's authenticator. Clients that do not implement hints ignore the field.
CREDENTIAL_HINTS = [PublicKeyCredentialHint.CLIENT_DEVICE, PublicKeyCredentialHint.HYBRID]

#: The same list as plain strings, for the ceremony this library version cannot
#: pass hints to directly (see `authentication_options`).
HINT_VALUES = [h.value for h in CREDENTIAL_HINTS]


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _store_challenge(key: str, challenge: bytes) -> None:
    try:
        get_redis().set(f"webauthn:{key}", _b64u(challenge), ex=CHALLENGE_TTL)
    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Challenge store unavailable")


def _pop_challenge(key: str) -> bytes:
    try:
        r = get_redis()
        val = r.get(f"webauthn:{key}")
        r.delete(f"webauthn:{key}")
    except redis.RedisError:
        raise HTTPException(status_code=503, detail="Challenge store unavailable")
    if not val:
        raise HTTPException(status_code=422, detail="Challenge expired — restart the passkey flow")
    return _from_b64u(val)


def registration_options(db: Session, user: User) -> dict:
    existing = db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
    ).scalars().all()
    s = get_settings()
    opts = generate_registration_options(
        rp_id=s.rp_id,
        rp_name=s.webauthn_rp_name,
        user_id=user.id.encode(),
        user_name=user.email,
        user_display_name=user.email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            # REQUIRED, not PREFERRED: the passkey login path issues a full
            # session on its own, so the ceremony has to actually carry the
            # second factor (device PIN or biometric) rather than merely
            # requesting one and accepting possession alone.
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=_from_b64u(c.credential_id)) for c in existing
        ],
        hints=CREDENTIAL_HINTS,
    )
    _store_challenge(f"reg:{user.id}", opts.challenge)
    return json.loads(options_to_json(opts))


def verify_registration(db: Session, user: User, credential: dict, nickname: str) -> WebAuthnCredential:
    s = get_settings()
    challenge = _pop_challenge(f"reg:{user.id}")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=s.rp_id,
            expected_origin=s.frontend_origin,
        )
    except InvalidRegistrationResponse as exc:
        raise HTTPException(status_code=422, detail=f"Passkey registration failed: {exc}")
    transports = credential.get("response", {}).get("transports") or []
    row = WebAuthnCredential(
        user_id=user.id,
        credential_id=_b64u(verified.credential_id),
        public_key=_b64u(verified.credential_public_key),
        sign_count=verified.sign_count,
        transports=",".join(transports)[:255] or None,
        nickname=(nickname or "Passkey")[:100],
    )
    db.add(row)
    db.commit()
    return row


def authentication_options() -> tuple[str, dict]:
    """Usernameless (discoverable credential) sign-in options.

    The hints are added to the serialized options rather than passed in:
    py_webauthn accepts `hints` on registration but not yet on authentication,
    and the field is part of the request the browser reads, not of anything
    that gets signed — so setting it here is equivalent and verifies the same.
    """
    s = get_settings()
    flow_id = secrets.token_urlsafe(24)
    opts = generate_authentication_options(
        rp_id=s.rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[],
    )
    _store_challenge(f"auth:{flow_id}", opts.challenge)
    payload = json.loads(options_to_json(opts))
    payload["hints"] = HINT_VALUES
    return flow_id, payload


def verify_authentication(db: Session, flow_id: str, credential: dict) -> User:
    s = get_settings()
    challenge = _pop_challenge(f"auth:{flow_id}")
    cred_id = credential.get("id") or ""
    row = db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.credential_id == cred_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown passkey")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=s.rp_id,
            expected_origin=s.frontend_origin,
            credential_public_key=_from_b64u(row.public_key),
            credential_current_sign_count=row.sign_count,
            # Enforced, not just requested: without this the flag the
            # authenticator sets is never checked and a non-verifying
            # authenticator signs in on possession alone.
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(status_code=401, detail=f"Passkey sign-in failed: {exc}")
    row.sign_count = verified.new_sign_count
    row.last_used_at = utcnow()
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account unavailable")
    db.commit()
    return user
