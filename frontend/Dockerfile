# Public map (Next.js) server image. Build context = the repository root (npm workspace: the
# frontend depends on packages/feasibility-engine), e.g.
#   docker build -f frontend/Dockerfile --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com .
# NEXT_PUBLIC_* values are inlined into the browser bundle at build time, so they are build args;
# API_INTERNAL_BASE_URL (server-side fetches, e.g. http://api:8000) is read at run time.

FROM node:22-bookworm-slim AS deps
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
COPY packages ./packages
COPY frontend/package.json ./frontend/package.json
# the root `prepare` script builds the shared feasibility engine
RUN npm ci

FROM deps AS build
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_MAPBOX_TOKEN=""
ARG NEXT_PUBLIC_MAPBOX_STYLE=mapbox://styles/mapbox/light-v11
ARG NEXT_PUBLIC_MARKET_DATA_FREE=false
ARG NEXT_PUBLIC_ANALYTICS_ENABLED=true
ARG NEXT_PUBLIC_DEFAULT_LANG=en
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_MAPBOX_TOKEN=$NEXT_PUBLIC_MAPBOX_TOKEN \
    NEXT_PUBLIC_MAPBOX_STYLE=$NEXT_PUBLIC_MAPBOX_STYLE \
    NEXT_PUBLIC_MARKET_DATA_FREE=$NEXT_PUBLIC_MARKET_DATA_FREE \
    NEXT_PUBLIC_ANALYTICS_ENABLED=$NEXT_PUBLIC_ANALYTICS_ENABLED \
    NEXT_PUBLIC_DEFAULT_LANG=$NEXT_PUBLIC_DEFAULT_LANG \
    NEXT_OUTPUT=standalone
COPY frontend ./frontend
RUN test -n "$NEXT_PUBLIC_API_BASE_URL" || (echo "NEXT_PUBLIC_API_BASE_URL build arg is required" && exit 1)
RUN npm run build -w @urbanview/frontend

FROM node:22-bookworm-slim AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# standalone server (traced from the repository root), then the static assets it does not copy
COPY --from=build --chown=node:node /app/frontend/.next/standalone ./
COPY --from=build --chown=node:node /app/frontend/.next/static ./frontend/.next/static
COPY --from=build --chown=node:node /app/frontend/public ./frontend/public
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "frontend/server.js"]
