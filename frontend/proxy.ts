import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = ["/login", "/signup", "/verify-email"];

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const hasSession = request.cookies.has("pt_access") || request.cookies.has("pt_refresh");

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    // /verify-email must work while signed in too (email-change confirmations)
    if (hasSession && !pathname.startsWith("/verify-email")) {
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
