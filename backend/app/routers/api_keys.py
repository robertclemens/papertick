from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Principal, require_manage
from app.models import ApiKey, utcnow
from app.schemas import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut
from app import security

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
def list_keys(principal: Principal = Depends(require_manage), db: Session = Depends(get_db)):
    """Lists this user's API keys, both active and revoked, newest first. Only
    metadata is returned — the plaintext key is never included, since it isn't
    stored anywhere after creation."""
    rows = db.execute(
        select(ApiKey).where(ApiKey.user_id == principal.user.id).order_by(ApiKey.created_at.desc())
    ).scalars().all()
    return [ApiKeyOut.model_validate(k) for k in rows]


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
def create_key(data: ApiKeyCreateIn, principal: Principal = Depends(require_manage),
               db: Session = Depends(get_db)) -> ApiKeyCreatedOut:
    """Creates a new API key with the given name and scopes, capped at 20 active
    keys per user. The plaintext key is returned exactly once in this response
    and cannot be retrieved again afterward — only its hash and prefix are
    persisted, so it must be saved now."""
    active = db.execute(
        select(ApiKey).where(ApiKey.user_id == principal.user.id, ApiKey.revoked_at.is_(None))
    ).scalars().all()
    if len(active) >= 20:
        raise HTTPException(status_code=422, detail="API key limit reached (20 active keys)")
    raw, key_hash, prefix = security.new_api_key()
    key = ApiKey(
        user_id=principal.user.id,
        name=data.name,
        key_hash=key_hash,
        prefix=prefix,
        scopes=",".join(sorted(set(data.scopes))),
    )
    db.add(key)
    db.commit()
    # raw is returned exactly once and never persisted in plaintext
    return ApiKeyCreatedOut(api_key=ApiKeyOut.model_validate(key), plaintext_key=raw)


@router.delete("/{key_id}", status_code=204)
def revoke_key(key_id: str, principal: Principal = Depends(require_manage),
               db: Session = Depends(get_db)) -> None:
    """Revokes an API key immediately, blocking any further use of it. Returns
    404 if the key does not exist or belongs to another user; revoking an
    already-revoked key is a no-op."""
    key = db.get(ApiKey, key_id)
    if key is None or key.user_id != principal.user.id:
        raise HTTPException(status_code=404, detail="API key not found")
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        db.commit()
