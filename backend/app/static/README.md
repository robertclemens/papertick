# Vendored documentation assets

Swagger UI and ReDoc are served from here rather than from a public CDN.

Loading them from `cdn.jsdelivr.net` meant an unauthenticated page on the API's
own origin executed third-party JavaScript that was pinned only to a floating
major version and carried no Subresource Integrity hash. A changed or hijacked
CDN response would have run with full access to that origin — including any API
key pasted into the Swagger "Authorize" dialog.

Serving them locally removes the third-party runtime dependency entirely, lets
the Content-Security-Policy stay `'self'`, and makes the docs work offline.

| file | upstream |
|---|---|
| `swagger-ui-bundle.js`, `swagger-ui.css` | `swagger-ui-dist@5.29.5` |
| `redoc.standalone.js` | `redoc@2.5.0` |

SHA-384 digests of the files as fetched are in `VENDOR.txt`. To upgrade: fetch
the new pinned version, refresh `VENDOR.txt`, and check the digests in review.
