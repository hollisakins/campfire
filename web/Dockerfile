# syntax=docker/dockerfile:1
# CAMPFIRE web portal, self-hosted image (see ops/README.md).
#
# Build from the web/ directory. Every NEXT_PUBLIC_* value is baked into the
# client bundle at build time, so a change to any of them is a rebuild, not a
# restart:
#
#   docker build -f web/Dockerfile \
#     --build-arg NEXT_PUBLIC_SUPABASE_URL=https://api.campfire.example \
#     --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=... \
#     --build-arg NEXT_PUBLIC_APP_URL=https://campfire.example \
#     --build-arg NEXT_PUBLIC_WORKER_DOWNLOAD_URL=https://campfire-download.example \
#     --build-arg GIT_SHA=$(git rev-parse HEAD) \
#     -t ghcr.io/hollisakins/campfire-web:$(git rev-parse HEAD) web/
#
# Node 20 matches .github/workflows/web.yml. Debian slim rather than Alpine so
# sharp (next/image) uses its prebuilt glibc binaries.

FROM node:20-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:20-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_WORKER_DOWNLOAD_URL
ARG NEXT_PUBLIC_DR_PAGES
ARG GIT_SHA=unknown
# Build-time placeholder only, mirroring CI; the builder stage is discarded,
# so nothing here reaches the runtime image. The real key arrives via .env.
ARG SUPABASE_SERVICE_ROLE_KEY=build-placeholder

ENV NEXT_PUBLIC_SUPABASE_URL=$NEXT_PUBLIC_SUPABASE_URL \
    NEXT_PUBLIC_SUPABASE_ANON_KEY=$NEXT_PUBLIC_SUPABASE_ANON_KEY \
    NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL \
    NEXT_PUBLIC_WORKER_DOWNLOAD_URL=$NEXT_PUBLIC_WORKER_DOWNLOAD_URL \
    NEXT_PUBLIC_DR_PAGES=$NEXT_PUBLIC_DR_PAGES \
    SUPABASE_SERVICE_ROLE_KEY=$SUPABASE_SERVICE_ROLE_KEY \
    NEXT_DEPLOYMENT_ID=$GIT_SHA \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-bookworm-slim AS runner
WORKDIR /app
ARG GIT_SHA=unknown
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    GIT_SHA=$GIT_SHA \
    NEXT_DEPLOYMENT_ID=$GIT_SHA

RUN groupadd --system --gid 1001 nodejs \
 && useradd --system --uid 1001 --gid nodejs --home /app nextjs

# The standalone output carries server.js plus the traced node_modules
# (sharp included); static assets and public/ are not traced and are copied
# beside it. outputFileTracingIncludes (next.config.ts) carries the updates
# markdown.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/api/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"
CMD ["node", "server.js"]
