# Runway web app (Next.js) for the hosted profile. Build from the repository root:
#   docker build --file deploy/web.Dockerfile --build-arg NEXT_PUBLIC_RUNWAY_API_URL=https://api.example.com .
# Pinned by digest (node:24-slim, read 2026-11-23); bump the tag and the digest together.
FROM node:24-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
# Public by design: the browser needs the API's address. Never pass a secret here.
ARG NEXT_PUBLIC_RUNWAY_API_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_RUNWAY_API_URL=${NEXT_PUBLIC_RUNWAY_API_URL}
# Dev dependencies stay: `next start` may still compile next.config.ts at boot.
RUN npm run build

FROM node:24-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=build --chown=node:node /app ./
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
