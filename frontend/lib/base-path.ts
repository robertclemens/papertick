/** Sub-folder deployments.
 *
 *  BASE_PATH is baked into the build (see next.config.mjs): empty when the app
 *  is served at a domain root, "/papertick" when a reverse proxy hands it a
 *  sub-folder of a shared domain.
 *
 *  `next/link`, `useRouter()` and `usePathname()` apply the prefix themselves,
 *  so ordinary navigation needs nothing. Raw `fetch()` calls, `<a href>` to a
 *  backend route, and `window.location` assignments do not — those go through
 *  `withBasePath()`.
 */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH || "";

/** Prefix an app-absolute path ("/api/v1/...") with the deployment base path. */
export function withBasePath(path: string): string {
  return `${BASE_PATH}${path}`;
}
