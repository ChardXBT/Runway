# Runway web app (Next.js) for the hosted profile. Build from the repository root:
#   docker build --file deploy/web.Dockerfile --build-arg NEXT_PUBLIC_RUNWAY_API_URL=https://api.example.com .
FROM node:24-slim AS build
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

FROM node:24-slim
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=build --chown=node:node /app ./
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
