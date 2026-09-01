/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_URL || "http://localhost:8000";
const dev = process.env.NODE_ENV !== "production";

// Sub-folder deployments: BASE_PATH="/papertick" serves the whole app under
// https://domain.example/papertick/ behind a reverse proxy that passes the
// prefix through untouched. Empty (the default) serves it at a domain root.
// Baked into the route manifest at build time, so a change needs `--build`.
const rawBase = (process.env.BASE_PATH || "").trim().replace(/\/+$/, "");
const basePath = rawBase && !rawBase.startsWith("/") ? `/${rawBase}` : rawBase;

// Next injects an inline bootstrap script and inline styles it does not
// nonce for us, so 'unsafe-inline' stays for now; everything else is pinned
// to this origin. There is no CDN in the page — the API docs serve Swagger
// and ReDoc from the backend's own /api/docs-assets.
const csp = [
  "default-src 'self'",
  // dev needs 'unsafe-eval' for React Fast Refresh; production does not.
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
  "manifest-src 'self'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "no-referrer" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

// Only meaningful over TLS, and dangerous to send from a plain-HTTP dev server
// (it would pin the browser to https for localhost).
if (process.env.COOKIE_SECURE === "true") {
  securityHeaders.push({
    key: "Strict-Transport-Security",
    value: "max-age=63072000; includeSubDomains; preload",
  });
}

const nextConfig = {
  output: "standalone",
  // Don't advertise the framework and its major version to a scanner.
  poweredByHeader: false,
  ...(basePath ? { basePath } : {}),
  // Inlined into the client bundle at build time. next/link, useRouter and
  // usePathname handle basePath themselves; raw fetch() calls and <a href> to
  // a backend route have to prefix it (see lib/base-path.ts).
  env: { NEXT_PUBLIC_BASE_PATH: basePath },
  async rewrites() {
    // Proxy the API through the frontend origin so httpOnly auth cookies are first-party.
    // Sources carry the basePath explicitly (hence `basePath: false`, so Next
    // does not prepend it a second time); the backend is always mounted at
    // /api, so the prefix is dropped on the way through.
    return [
      { source: `${basePath}/api/:path*`, destination: `${backend}/api/:path*`, basePath: false },
      { source: `${basePath}/healthz`, destination: `${backend}/healthz`, basePath: false },
    ];
  },
  async headers() {
    return [{ source: `${basePath}/:path*`, headers: securityHeaders, basePath: false }];
  },
};

export default nextConfig;
