FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# `npm ci` installs exactly the lockfile, and fails if package.json and the
# lockfile disagree. `npm install` would silently resolve new versions at build
# time, so the image would not match what was reviewed.
RUN npm ci --no-audit --no-fund

FROM node:24-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG BACKEND_URL=http://backend:8000
# Sub-folder deployments (e.g. BASE_PATH=/papertick). Empty = domain root.
# Both are compiled into the build output, so changing either needs a rebuild.
ARG BASE_PATH=""
ENV NEXT_TELEMETRY_DISABLED=1 BACKEND_URL=$BACKEND_URL BASE_PATH=$BASE_PATH
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
RUN addgroup -S app && adduser -S app -G app
COPY --from=builder --chown=app:app /app/.next/standalone ./
COPY --from=builder --chown=app:app /app/.next/static ./.next/static
COPY --from=builder --chown=app:app /app/public ./public
USER app
EXPOSE 3000
CMD ["node", "server.js"]
