FROM oven/bun:1-slim AS builder

WORKDIR /app

# Install dependencies first for layer caching
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

# Install admin dependencies
COPY admin/package.json ./admin/
RUN cd admin && bun install

# Copy source and build the standalone binary + admin UI
COPY . .
RUN bun run build && bun run build:admin
RUN ls -R admin/dist

FROM debian:bookworm-slim

WORKDIR /app

# Install certificates and basic utils
RUN apt-get update && apt-get install -y ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Copy the compiled binary from the builder
COPY --from=builder /app/bin/gbrain /usr/local/bin/gbrain

# Copy the admin dashboard assets
# We copy them to /app/admin/dist which matches process.cwd() + 'admin/dist'
COPY --from=builder /app/admin/dist /app/admin/dist

# Set the default port for Cloud Run
ENV PORT=8787
EXPOSE 8787

# Run the HTTP server. Migrations are NOT run here — see DEPLOY.md.
# Migration v24 (RLS backfill) requires BYPASSRLS, which the runtime gbrain
# role intentionally lacks. Migrations are run from a workstation against the
# DB as the postgres (or another BYPASSRLS) role before each deploy.
CMD ["gbrain", "serve", "--http", "--port", "8787"]
