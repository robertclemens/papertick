# Reverse proxy

The `frontend` container is the **only** thing a reverse proxy needs to point at.
Its Next.js rewrites (`frontend/next.config.mjs`) already forward `/api/*` and
`/healthz` to the `backend` service over the internal Docker network — that's also
why auth cookies work as first-party (see [auth and security](auth-and-security.md)).
So one upstream, one TLS certificate, one vhost: the web UI, the OpenAPI docs
(`/api/docs`, `/api/redoc`), and every `/api/v1/...` call an agent makes all arrive
through `frontend:3000`. There is no separate proxy path to configure for
`backend:8000` — compose does not publish it at all.

If the proxy runs on the same host as `docker compose`, bind the published port to
loopback so only the proxy — not the world — can reach it directly, e.g. in a
`docker-compose.override.yml`:

```yaml
services:
  frontend:
    ports:
      - "127.0.0.1:3000:3000"
```

Whichever proxy you use, forward `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto`.
The app functions without them — `FRONTEND_ORIGIN` is a static setting, not derived
from request headers — but they are what upstream logging and client IPs are built
from, and `X-Forwarded-For` is required if you ever set `TRUSTED_PROXY_CIDRS`.
Get a certificate from Let's Encrypt (or any ACME CA) for the hostname you serve;
Caddy does this automatically.

## Two shapes

- **Own hostname** — `https://papertick.example`. Nothing extra to configure; this
  is the default.
- **Sub-folder of a shared domain** — `https://domain.example/papertick/`. Set
  `BASE_PATH=/papertick` in `.env` and rebuild (`docker compose up -d --build`):
  the prefix is compiled into the frontend bundle and route manifest, so a plain
  restart will not pick it up. The proxy must then pass the prefix through
  **unchanged** — do not strip it. Each config below shows both shapes.

`FRONTEND_ORIGIN` stays a bare origin in both cases (`https://domain.example`) —
an `Origin` header never carries a path, and CORS, the cross-origin guard and
WebAuthn all compare against it.

> **Sub-folder trade-off.** Cookies and passkeys are scoped to a *host*, not a
> path. On a shared domain the WebAuthn relying party is `domain.example`, so a
> passkey registered for PaperTick is offered by every app on that domain, and
> any of them can set cookies PaperTick will receive. PaperTick scopes its own
> session cookie down to `BASE_PATH`, but that is defense in depth, not isolation.
> A dedicated hostname is the stronger boundary; use a sub-folder when sharing a
> certificate or a single public entry point matters more.

## Caddy

Own hostname:

```caddyfile
yourdomain.example {
    reverse_proxy 127.0.0.1:3000
}
```

Sub-folder — `handle` (not `handle_path`, which would strip the prefix), with a
matcher that covers `/papertick` itself as well as everything under it:

```caddyfile
domain.example {
    @papertick path /papertick /papertick/*
    handle @papertick {
        reverse_proxy 127.0.0.1:3000
    }

    # whatever else this domain serves
    handle {
        reverse_proxy 127.0.0.1:8080
    }
}
```

Caddy provisions and renews the TLS certificate itself and sets the forwarding
headers above by default.

## nginx

Own hostname:

```nginx
server {
    listen 80;
    server_name yourdomain.example;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name yourdomain.example;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.example/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Sub-folder — inside the existing `443` server block for `domain.example`:

```nginx
    # ^~ so a regex location elsewhere in the vhost cannot claim these URLs.
    location ^~ /papertick {
        # No path on the proxy_pass URL: nginx then forwards the request URI
        # untouched. Adding a trailing "/" would strip /papertick and every
        # asset, route and API call underneath it would 404.
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

Provision the certificate with `certbot --nginx -d yourdomain.example` (or your
ACME client of choice) before the `443` block will start.

## Apache httpd

Requires `proxy`, `proxy_http`, `ssl`, `headers`, and `rewrite` enabled
(`a2enmod proxy proxy_http ssl headers rewrite` on Debian/Ubuntu).

Own hostname:

```apacheconf
<VirtualHost *:80>
    ServerName yourdomain.example
    RewriteEngine On
    RewriteCond %{HTTPS} off
    RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]
</VirtualHost>

<VirtualHost *:443>
    ServerName yourdomain.example

    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/yourdomain.example/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/yourdomain.example/privkey.pem

    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:3000/
    ProxyPassReverse / http://127.0.0.1:3000/
    RequestHeader set X-Forwarded-Proto "https"
</VirtualHost>
```

Sub-folder — inside the existing `443` vhost for `domain.example`. The prefix
appears on **both** sides of `ProxyPass`, which is what keeps it on the
forwarded request:

```apacheconf
    ProxyPreserveHost On
    ProxyPass        /papertick http://127.0.0.1:3000/papertick
    ProxyPassReverse /papertick http://127.0.0.1:3000/papertick
    RequestHeader set X-Forwarded-Proto "https"
```

`ProxyPreserveHost` supplies `Host`; `mod_proxy` sets `X-Forwarded-For` on its own.

## Then update .env

In `.env`, set `COOKIE_SECURE=true`, point `FRONTEND_ORIGIN` at the public origin,
and add `BASE_PATH` if you went the sub-folder route:

```dotenv
# own hostname
FRONTEND_ORIGIN=https://yourdomain.example
COOKIE_SECURE=true
BASE_PATH=

# sub-folder
FRONTEND_ORIGIN=https://domain.example
COOKIE_SECURE=true
BASE_PATH=/papertick
```

`ALLOWED_HOSTS` does **not** take your public hostname, and does not change when
the public address does. The frontend proxies `/api` to the backend with
`changeOrigin`, which rewrites `Host` to the upstream's own name — so the backend
only ever sees `Host: backend:8000` from that rewrite, or `127.0.0.1:8000` from
its own healthcheck. Put your public hostname there and every API call comes back
`400 Invalid host header`, which the UI surfaces as `Request failed (400)`. Leave
it at `backend,127.0.0.1`.

Rejecting a spoofed *public* Host is the proxy's job, and each config above
already does it: they match one hostname and answer anything else with a 404.
That only holds while the proxy is the sole way in, which is why the published
frontend port should be bound to loopback.

Then rebuild — `docker compose up -d --build`. All three matter before cookies,
CORS and WebAuthn will accept the new origin (see
[Deploying to production](../README.md#deploying-to-production)), and `BASE_PATH` only takes
effect through a rebuild.

Agents then use `https://yourdomain.example/api/v1/...` (or
`https://domain.example/papertick/api/v1/...`) in place of `localhost:3000` in the
[API examples](api.md) — same routes, same responses,
just through the proxied origin.

