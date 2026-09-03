# Authentication and security

## Accounts & sign-in

Sign-in runs in stages: **email → password or passkey → a second step, if the
account calls for one.**

- **Passkeys (WebAuthn)** — register any number and sign in with a device screen
  lock, biometric, or a third-party password manager. Discoverable credentials,
  so no username is typed. Requires a secure context (HTTPS, or `localhost` in
  development). Both ceremonies send WebAuthn `hints` of `client-device` and
  `hybrid`: without them iOS cannot tell a passkey request from a
  hardware-security-key request and leads with "Security Key", so a user whose
  passkeys live in Bitwarden or 1Password is prompted for hardware they do not
  have. Hints steer the picker without excluding anyone — a security key still
  works, and so does scanning a QR code from another device. (On iOS the manager
  must also be enabled under Settings → General → AutoFill & Passwords.)
  A passkey sign-in **completes on its own, even when TOTP is also enrolled**:
  both ceremonies run with `user_verification=REQUIRED` and enforce the flag on
  the response, so it already proves possession of the authenticator *and* the
  biometric or PIN that unlocked it — two factors, AAL2 under NIST SP 800-63B,
  and origin-bound on top. Chaining a phishable code behind an unphishable
  credential adds nothing and taxes the stronger method.
- **Optional MFA** — TOTP authenticator apps with QR enrollment. Never required:
  each account chooses passkeys, TOTP, both, or neither. When enrolled, it gates
  the **password** path.
- **Optional passwordless** — an account with **two or more** passkeys can turn
  password sign-in off entirely, after which a correct password is refused. Two
  is the floor because one authenticator with no password behind it is a single
  point of failure; the same floor blocks removing a passkey that would drop the
  account below it. The password hash is kept, so the switch is reversible from
  Settings.
- **New-device verification** — what a **password** sign-in must clear on a
  browser this account has not used before. In production it applies to every
  password sign-in, *whether or not the account also holds a passkey or an
  authenticator*: those are credentials the sign-in could have used, not ones it
  did, and crediting a passkey's strength to a sign-in that never touched it is
  factor confusion. A six-digit code is emailed to the account; verified browsers
  are remembered for `DEVICE_TRUST_DAYS` and skip it. TOTP is asked for earlier
  on the same path, and a passkey sign-in — being origin-bound and already two
  factors — never reaches this step at all. Only the SHA-256 of the device token
  is stored, and the code lives in Redis with a five-attempt budget, never in the
  database. Development is exempt, or a fresh checkout with no SMTP relay could
  not be logged into at all. Remembered browsers are listed and revocable under
  Settings.
- **Forgotten password** — `/forgot-password` emails a single-use link. The token
  is 256 bits, stored only as its SHA-256, and expires in
  `PASSWORD_RESET_TTL_SECONDS` (30 minutes). Requesting a new link burns the
  previous one, redeeming one burns every other, and a *completed* sign-in
  cancels them all — so "if this wasn't you, ignore it" is literally true. It is
  the completed sign-in, not the correct password, on purpose: otherwise someone
  holding a stolen password but not the second factor could cancel the owner's
  recovery links on demand. The response is identical for a registered and an unregistered address,
  so the endpoint cannot be used to test who has an account. A completed reset
  revokes every session and forgets every remembered browser, and does **not**
  sign the caller in: they sign in with the new password, which proves the reset
  landed on the account they meant. Rate-limited per source address *and* per
  target address, since either alone is bypassable.
- **Email verification** — required in `ENV=production` for signup and for email
  changes (the address only changes once the link is clicked); skipped in
  development. With no SMTP configured, links are written to the backend log.
- **Profile edits** — change email (password-confirmed) and date of birth. A
  birthdate change is checked against contribution history first and must be
  confirmed if it would move you across the age-50 catch-up threshold or turn a
  past contribution into an over-contribution.

## Security activity and email notices

Every security-relevant event is recorded against the account with the
originating IP address, the browser and platform family, and what changed —
sign-ins, lockouts, password and email changes, passkeys added or removed,
authenticator and passwordless toggles, API keys created or revoked. Settings →
**Security activity** lists them; rows are pruned after 400 days.

The ones a user would want to know about are also emailed, in a short branded
message that states what happened, when, from which IP and on what device. An
email change names **both** addresses — the one on file and the one requested —
and the notice goes to the address currently on file, which is the one that
would be losing access; the confirmation, once the change lands, goes to both.
Emails are multipart (plain text and HTML), load nothing from the network, and
never carry a password or a code the recipient did not ask for.

Getting the IP right needs `TRUST_PROXY_HEADERS` on the frontend and
`TRUSTED_PROXY_CIDRS` on the backend — see [reverse-proxy.md](reverse-proxy.md).

## Security posture

- Argon2id password hashing (64 MiB, t=3) with strength checks and rehash-on-login.
- Short-lived JWT access tokens + rotating refresh tokens (hashed at rest,
  reuse detection revokes the whole session family).
- httpOnly, SameSite=Lax cookies, first-party via the Next.js proxy; `Secure` when
  `COOKIE_SECURE=true` behind TLS. The refresh cookie is scoped to the one route
  that consumes it, and a server-side origin check backs up SameSite on every
  cookie-authenticated state change.
- TOTP MFA — secret encrypted at rest (Fernet key HKDF-derived from `SECRET_KEY`),
  QR enrollment, password + code required to disable.
- Redis rate limiting on auth/trading endpoints; login lockout after repeated failures;
  timing-equalized login for unknown emails. Redis itself requires a password, so a
  process that reaches the network cannot clear a lockout or set a fill price.
- API keys stored as SHA-256 hashes; plaintext shown exactly once; scoped; revocable.
- The backend port is never published: only `frontend` is reachable from the host,
  so there is no route around TLS or the ingress. `ALLOWED_HOSTS` pins the backend
  to the Host values it legitimately sees (`backend`, `127.0.0.1`) — a spoofed
  *public* Host is rejected by the reverse proxy, which matches one hostname.
  `X-Forwarded-For` is believed only from CIDRs you list in `TRUSTED_PROXY_CIDRS`
  (nothing, by default) and only when the frontend has been told a real proxy is
  in front (`TRUST_PROXY_HEADERS`, false by default), and the chain is read from
  the right — so a caller cannot choose its own rate-limit bucket or its own
  entry in the security log.
- Strict Pydantic validation with bounds on every money/shares input; request bodies
  capped at 8 MiB before parsing; only US-listed USD symbols the market-data source
  can confirm become tradable. Security headers and a strict CSP on both services;
  Swagger UI and ReDoc are served from the backend's own origin rather than a CDN;
  non-root containers with `no-new-privileges` and dropped capabilities.
- In `ENV=production` the API docs, ReDoc and the OpenAPI spec are served only to an
  authenticated caller — the full map of the attack surface is not handed to
  anonymous visitors.

