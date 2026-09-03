import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = [
  "/login",
  "/signup",
  "/verify-email",
  // Password recovery is reached from an email, by someone who by definition
  // cannot sign in. Leaving these out bounces them to /login, which is the one
  // page that cannot help them.
  "/forgot-password",
  "/reset-password",
];

// Pages that must render even for a signed-in browser. A reset link is often
// opened on the device that is still signed in as the account being recovered
// — and after a reset every session is revoked anyway, so redirecting a
// "logged in" visitor to the dashboard would strand them on a dead session.
const ALWAYS_PUBLIC = ["/verify-email", "/reset-password"];

// The backend prefixes its session cookies when COOKIE_SECURE is on —
// `__Host-` pins the access cookie to the exact origin, and the refresh
// cookie takes `__Secure-` because `__Host-` would forbid its narrower
// Path (see backend app/deps.py). Over plain HTTP the bare names are used.
// Both spellings have to be matched here: this file is compiled into the
// image and never sees COOKIE_SECURE, and recognising only the bare names
// bounces every signed-in request straight back to /login in production.
const SESSION_COOKIES = [
  "pt_access",
  "pt_refresh",
  "__Host-pt_access",
  "__Secure-pt_refresh",
];

/** Whether something in front of this server is authoritative for
 *  `X-Forwarded-For`.
 *
 *  Next fills the header in from the socket when the request arrives without
 *  one, but leaves a client-supplied value exactly as sent — it does not
 *  append itself to the chain. So the header is trustworthy only when a real
 *  reverse proxy has already normalised it, and is pure client input when this
 *  server is reachable directly.
 *
 *  Off by default. Read per request rather than captured at module scope, so
 *  it is a plain runtime setting: no rebuild, just restart the container.
 */
function trustProxyHeaders(): boolean {
  return process.env.TRUST_PROXY_HEADERS === "true";
}

/** Headers a client must not be able to set for itself. */
const CLIENT_ADDRESS_HEADERS = ["x-forwarded-for", "x-real-ip"];

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // API traffic is proxied straight through to the backend; the only thing to
  // do here is make sure the caller cannot dictate its own source address.
  // The backend records that address in the security log and keys its rate
  // limiters on it, so a forged header would let one caller wear another's
  // identity in both.
  //
  // With nothing trusted in front, the header is stripped: the backend then
  // falls back to the peer address, which is this container. That is a less
  // useful answer than the client's real address, and it is the correct one —
  // there is no way to tell a genuine hop from a forged one here.
  if (pathname.startsWith("/api")) {
    if (trustProxyHeaders()) return NextResponse.next();
    const headers = new Headers(request.headers);
    for (const name of CLIENT_ADDRESS_HEADERS) headers.delete(name);
    return NextResponse.next({ request: { headers } });
  }

  const hasSession = SESSION_COOKIES.some((name) => request.cookies.has(name));

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    // A cookie proves only that a cookie exists — the token inside it can be
    // revoked, expired, or signed with a retired SECRET_KEY. The API client
    // sends ?expired=1 when it has already tried a refresh and been refused,
    // so honour that instead of bouncing a dead session back to a page that
    // will immediately send it here again.
    const expired = request.nextUrl.searchParams.has("expired");
    if (hasSession && !expired &&
        !ALWAYS_PUBLIC.some((p) => pathname.startsWith(p))) {
      return NextResponse.redirect(new URL("/", request.url));
    }
    return NextResponse.next();
  }
  if (!hasSession) {
    const url = new URL("/login", request.url);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Page routes are a UX guard only — real authorization happens in the API on
  // every request. /api is matched too, but solely to normalise the forwarded
  // client address before the request is proxied onward.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
