import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = ["/login", "/signup", "/verify-email"];

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

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const hasSession = SESSION_COOKIES.some((name) => request.cookies.has(name));

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    // A cookie proves only that a cookie exists — the token inside it can be
    // revoked, expired, or signed with a retired SECRET_KEY. The API client
    // sends ?expired=1 when it has already tried a refresh and been refused,
    // so honour that instead of bouncing a dead session back to a page that
    // will immediately send it here again.
    const expired = request.nextUrl.searchParams.has("expired");
    // /verify-email must work while signed in too (email-change confirmations)
    if (hasSession && !expired && !pathname.startsWith("/verify-email")) {
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
  // UX guard only — real authorization happens in the API on every request.
  matcher: ["/((?!api|_next/static|_next/image|healthz|favicon.ico).*)"],
};
