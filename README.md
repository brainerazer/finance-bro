# finance-bro

Self-hosted personal-finance tool. Pulls Monobank transactions, stores them on
hardware you control, exposes a JSON API. **Phase 1 (walking skeleton):** import
one card, read transactions back. No UI yet. No app-level auth — gated by your
LAN / Tailscale.

## Requirements

- Docker + docker compose
- A Monobank personal API token (https://api.monobank.ua)
- About 200 MB of disk

## Setup

1. `cp .env.example .env`
2. Open `.env` and fill in `MONO_TOKEN` and `POSTGRES_PASSWORD`. Generate a
   password with `openssl rand -base64 32`.
3. `docker compose up -d`

The app is reachable at `http://localhost:8000/docs` (Swagger UI). Drive the API
from there: click `POST /api/import`, "Try it out", "Execute". Then click
`GET /api/transactions`, "Execute". Real Mono rows.

The first import takes up to ~65 seconds because the app blocks on Mono's
1-request-per-60-second rate limit between `/personal/client-info` and
`/personal/statement`.

## Network egress

The app contacts ONE external host: `api.monobank.ua` (HTTPS). No analytics,
no telemetry, no third-party SDKs. `grep -E '(sentry|posthog|mixpanel|segment)'
pyproject.toml package.json` returns nothing. NBU FX rates land in Phase 3, at
which point `bank.gov.ua` is added.

## Rotation: changing the Mono token

Edit `.env`, change `MONO_TOKEN`, then `docker compose up -d`. The new token is
read into the process on restart. The old token is gone — the app never
persisted it.

## Backup & restore (Phase 1 minimum)

The Postgres data lives in `${DATA_DIR}/postgres` as a bind mount. To back up:
`tar -czf finance-bro-backup-$(date +%F).tgz ${DATA_DIR}/postgres`. To restore:
stop the stack (`docker compose down` — **NEVER `down -v`**), restore the
directory, `docker compose up -d`. A daily `pg_dump` cron lands in Phase 7.

**Important:** never run `docker compose down -v` — the `-v` flag wipes named
volumes. The bind mount survives `down`, but `down -v` is still in muscle
memory and the bind mount path may not be enough protection. Treat `-v` as
forbidden in this stack.

## PUID / PGID (Synology / Unraid)

The container runs as UID 1000 / GID 1000 (set via `user: "1000:1000"` in
compose.yml). On Synology / Unraid, ensure the user that owns
`${DATA_DIR}/postgres` matches. Adjust the `user:` line in compose.yml if
your NAS expects a different UID.

## Trust model

finance-bro has **no app-level authentication** in v1. It binds to
`127.0.0.1:8000` only — anything reaching it has already passed your firewall.
Reachable from your LAN devices via the host machine's IP, or remotely via
Tailscale Funnel. Do **NOT** expose the app to the public internet without
reading the v2 auth roadmap first.

## Logs

`docker compose logs -f app` shows structured JSON. The structlog redaction
processor masks the Mono token, the `X-Token` header, and any field whose name
matches `/token|amount/i` at INFO level. To diagnose with raw values, set
`LOG_LEVEL=DEBUG` in `.env` and restart — but never with a real token.

## What Phase 1 does NOT do (yet)

- No automatic polling — you click `POST /api/import` (Phase 2)
- No UAH rollup for USD/EUR transactions (Phase 3)
- No category, no rules engine (Phase 4)
- No transfer / refund netting (Phase 5)
- No web UI — Swagger UI is the entry point (Phase 6)
- No automated backup, no CSV export (Phase 7)

See `.planning/ROADMAP.md` for the full phase plan.
