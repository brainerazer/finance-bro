---
phase: 01-first-real-transaction
plan: 04
subsystem: deploy
tags: [docker, docker-compose, postgres-17, uv-multistage, alembic-on-startup, real-mono-validation]

requires:
  - phase: 01-first-real-transaction
    plan: 01
    provides: "Schema + Alembic 0001 migration, structlog redaction processor, async engine, Settings, testcontainers harness."
  - phase: 01-first-real-transaction
    plan: 02
    provides: "MonobankImporter (real httpx adapter with X-Token, no token in URL), persistent RateLimitGate (65s cadence, FOR UPDATE)."
  - phase: 01-first-real-transaction
    plan: 03
    provides: "ImportService.run_one_card, FastAPI routes (/api/health, /api/accounts, /api/transactions, /api/import), Pydantic schemas."

provides:
  - "compose.yml — postgres:17-bookworm + finance-bro-app, bind-mount under ${DATA_DIR:-./data}/postgres, bound to 127.0.0.1:8000, app runs as user 1000:1000, db service_healthy gate, no top-level volumes block"
  - "Dockerfile — two-stage: builder (python:3.13-slim-trixie + uv 0.11.12 from ghcr) does deps-only sync first, then `COPY src/ src/` + project install for layer caching; runtime (python:3.13-slim-trixie) copies the venv, runs as `app` (UID 1000), `alembic upgrade head && uvicorn ... --workers 1`"
  - "README.md — operator guide: setup (.env, MONO_TOKEN, POSTGRES_PASSWORD, DATA_DIR), `docker compose up`, Tailscale-only access model, explicit `down -v` ban, links to /docs and /api/import"
  - ".env.example — commented stub for MONO_TOKEN, POSTGRES_PASSWORD, DATA_DIR with one-line explanations per variable"

affects: [02-multi-account-and-backfill, 03-fx-and-categorization]

tech-stack:
  added: [docker-compose, postgres-17-bookworm, uv-prebuilt-image-0.11.12]
  patterns:
    - "uv multi-stage Docker: pass 1 syncs deps with `--no-install-project` (cached layer), pass 2 syncs the project after `COPY src/` (rebuilds only when src changes). Project ends up installed in the venv so `alembic env.py` can `from finance_bro.core.settings import get_settings`."
    - "alembic-on-startup: container CMD chains `alembic upgrade head && uvicorn ...`. Single-user deploy, no orchestration layer — schema migrates lazily on each container start. Postgres healthcheck gates the app start so alembic doesn't race the DB readiness."
    - "Bind mount, not named volume: `${DATA_DIR:-./data}/postgres:/var/lib/postgresql/data`. The user can `tar`/rsync that directory for offsite backup; no `docker volume` indirection. Top-level `volumes:` block intentionally omitted (verified by grep gate)."
    - "127.0.0.1 binding: `127.0.0.1:8000:8000` (not `0.0.0.0`). LAN/Tailscale-gated by deployment topology — the Docker-published port is loopback-only on the host. Documented in README as a hard constraint, not a default."
  removed: []

verification:
  - "Task 1 grep gates (Dockerfile + compose.yml): `postgres:17-bookworm` ×1, `127.0.0.1:8000:8000` ×1, `${DATA_DIR:-./data}/postgres` ×1, top-level `^volumes:` ×0, `alembic upgrade head` ×1, `--workers 1` ×1, `user: \"1000:1000\"` ×1, `service_healthy` ×1 — all pass"
  - "Task 1 README content gates: contains `docker compose up`, `MONO_TOKEN`, `POSTGRES_PASSWORD`, `DATA_DIR`, `127.0.0.1`, `Tailscale`, `down -v` (with NEVER), `Phase 1`, `api.monobank.ua` — all present"
  - "Task 1 `MONO_TOKEN=dummy POSTGRES_PASSWORD=dummy docker compose -f compose.yml config` exits 0"
  - "SC#1 — `docker compose up -d` brings both services to healthy. `curl -fsS http://localhost:8000/docs` returns 200; Swagger UI loads in browser. `/api/health` returns `{\"status\":\"ok\",\"db\":\"ok\"}`."
  - "SC#2 (real Mono) — `POST /api/import` against live api.monobank.ua: discovered 5 real cards via /personal/client-info, polled USD black card, inserted 9 real transactions. Row shape verified: `amount_minor` is signed int, `currency` is 3-letter alpha, `time` is ISO-8601 UTC, `raw_payload` is verbatim Mono `statementItem` (`amount`, `balance`, `cashbackAmount`, `commissionRate`, `currencyCode`, ...)."
  - "SC#3 (live idempotency) — second POST blocked ~45s on the persistent rate gate (correct), returned `inserted:0, skipped_duplicates:9`. Transaction count remained 9 — partial unique index on `(account_id, source_tx_id) WHERE NOT is_deleted` held."
  - "SC#5 (logs clean) — full container log scan with `grep -ciE '(token[^_]|x-token|amount[^_])'` returned **0** (the negative classes deliberately exempt safe field NAMES like `amount_minor`, `token_hash`, `account_id`)."

deviations:
  - rule: 1
    reason: "Builder used `uv sync --frozen --no-dev --no-install-project` only, which installs locked dependencies but skips the local `finance-bro` package itself. At runtime, `alembic env.py` does `from finance_bro.core.settings import get_settings` and crashed with `ModuleNotFoundError: No module named 'finance_bro'`, putting the app container in a CrashLoop. Discovered during SC#1 attempt — `docker compose ps` showed `Restarting (1) 22 seconds ago`."
    fix: "Two-pass sync: keep the deps-only line for layer caching, then `COPY src/ src/` + a second `uv sync --frozen --no-dev` so the project gets installed into `.venv`. Also added `README.md` to the first COPY since pyproject.toml's `readme` field is read by the build backend. Committed as `0a73683` — `fix(01-04): install project in builder so alembic can import finance_bro`."
    impact: "None — preserves intent of cached deps layer while fixing the missing project install. Image size unchanged (project install adds a `.pth` file, not new artifacts)."

empirical_observations:
  mono_account_types_seen: ["eAid", "black", "platinum", "white"]
  mono_fop_seen: false
  mono_429_seen: false
  notes:
    - "5 cards returned by /personal/client-info under the user's personal token: 1× eAid (Aid for Ukraine charity card, no recent activity), 2× black (USD + EUR), 1× platinum (UAH, dual PAN), 1× white (UAH)."
    - "First-by-id account selection (D-04 from the plan) picked the eAid card on first run — empty 31-day statement window, valid Mono response. Phase 2 should make 'which card to poll' configurable or skip eAid/inactive types; for the SC#2 demonstration we deleted the eAid row from the accounts table and re-imported, which then picked the USD black card and returned 9 real statementItems."
    - "Rate gate held cleanly across two real round-trips: zero 429s observed in either direction. Second POST blocked ~45s on the gate (the elapsed budget at that point was ~16s of the 65s window)."
    - "`amount_minor` came back as a signed int directly from Mono's minor-unit payload — first row had `-999` (USD outflow), confirming the boundary conversion never floats."

key_files:
  created:
    - "compose.yml"
    - "Dockerfile"
    - ".env.example"
  modified:
    - "README.md (was empty placeholder; now operator guide)"

commits:
  - hash: d0bb685
    msg: "feat(01-04): add compose.yml, Dockerfile, README, .env.example for Phase 1 deploy"
  - hash: 0a73683
    msg: "fix(01-04): install project in builder so alembic can import finance_bro"

requirement_ids: [DEP-01, DEP-02, OPS-01, OPS-04]
---

## Summary

Phase 1's deployable artifact. `docker compose up -d` brings up Postgres 17 + the FastAPI app on `127.0.0.1:8000`, alembic migrates on startup, the app polls one Mono card via the persistent rate gate, and `/docs` exposes the read API.

Validated end-to-end against real `api.monobank.ua`: 5 real accounts discovered, 9 real transactions inserted from the USD black card, idempotent re-import, zero token/X-Token/amount leakage in container logs.

## What was built

**Container topology (compose.yml).** Two services. `db` is `postgres:17-bookworm` with a healthcheck and a bind-mount under `${DATA_DIR:-./data}/postgres` — no named volume so the operator can `tar` the directory directly for backup. `app` builds from the local `Dockerfile`, runs as `user: "1000:1000"` (matches the bind-mount owner on Synology/Unraid), is bound to `127.0.0.1:8000` (loopback only — Tailscale or the LAN handles access control), and gates startup on `service_healthy: db`.

**Image (Dockerfile).** Two-stage. Stage 1 (`builder`) is `python:3.13-slim-trixie` with `uv` copied from `ghcr.io/astral-sh/uv:0.11.12` — first pass syncs locked deps with `--no-install-project` (cached layer), then `COPY src/ src/` and a second `uv sync` installs the `finance-bro` project itself so alembic can import it. Stage 2 (`runtime`) is the same slim base with `curl` (for the compose healthcheck), a non-root `app` user (UID 1000), the venv copied from the builder, and a CMD that chains `alembic upgrade head && uvicorn finance_bro.main:app --host 0.0.0.0 --port 8000 --workers 1`. Single worker — single-user, single-token, single APScheduler instance is the model.

**Operator docs (README.md, .env.example).** README covers the four-command setup (`cp .env.example .env` → fill MONO_TOKEN + generate POSTGRES_PASSWORD → `mkdir -p ./data/postgres` → `docker compose up -d`), explains the Tailscale-only access model, calls out the `docker compose down -v` ban (would erase the bind-mounted Postgres data), points to `/docs` and `POST /api/import`, and links to `api.monobank.ua` for token provisioning. `.env.example` is a 16-line stub with one comment per variable explaining what it is and why.

## How it was verified

**Static gates (Task 1).** All grep invariants from the plan hold: `postgres:17-bookworm` ×1, `127.0.0.1:8000:8000` ×1, bind-mount string ×1, no top-level `volumes:` block, `alembic upgrade head` ×1, `--workers 1` ×1, `user: "1000:1000"` ×1, `service_healthy` ×1. `docker compose -f compose.yml config` exits 0 with dummy env vars.

**SC#1 — stack up.** After the `0a73683` Dockerfile fix: `docker compose up -d` → both services healthy in ~12s. `curl -fsS http://localhost:8000/docs` returns 200; Swagger UI loads with no auth challenge. `/api/health` returns `{"status":"ok","db":"ok"}` (DB roundtrip works). `alembic.runtime.migration: Running upgrade -> 0001, walking skeleton` appears once on first startup.

**SC#2 — real Mono returns real rows.** First `POST /api/import` discovered 5 accounts via `/personal/client-info` (real Mono response). Picked the first card by id — happened to be the `eAid` card — and got `statement_count: 0` (valid empty response from Mono for a charity card with no activity). To prove the write path, deleted the eAid account row and re-imported (rate gate had elapsed >65s, so no wait). Second call polled the USD black card and inserted 9 real transactions. Sample row from `/api/transactions`:

```json
{"id":1,"account_id":2,"source_tx_id":"Mpkdu1lgwc-zlr43xA","time":"2026-05-08T09:21:44Z",
 "amount_minor":-999,"currency":"USD","raw_payload":{"amount":...,"balance":...,
 "cashbackAmount":...,"commissionRate":...,"currencyCode":...}}
```

`amount_minor` is signed int (no float drift), `currency` is alpha-3, `time` is ISO-8601 UTC, `raw_payload` is the verbatim Mono `statementItem` — every threat-model invariant from Plans 01–03 holds end-to-end.

**SC#3 — live idempotency.** Immediate second `POST /api/import` blocked ~45s on the persistent rate gate (it had ~20s of the 65s budget elapsed at call time), then returned `{"polled_account_id":"21DY...", "statement_count":9, "inserted":0, "skipped_duplicates":9}`. `/api/transactions` count stayed at 9. The `(account_id, source_tx_id) WHERE NOT is_deleted` partial unique index held.

**SC#5 — logs clean.** Full container log scan after both imports:
`docker logs finance-bro-app-1 2>&1 | grep -ciE '(token[^_]|x-token|amount[^_])'` returns **0**. The `[^_]` negative classes deliberately exempt safe field NAMES (`amount_minor`, `token_hash`, `account_id`); a real leak (the literal token string, an `X-Token: ...` header, or a raw amount float) would still match. Logs at INFO show only request lines and the alembic startup banner.

## Open questions resolved (and one created)

| ID | Question | Answer |
|---|---|---|
| OQ-1 | Will `mono.fop` appear under a personal token? | **No** — not seen in this user's account list. Phase 2 may revisit if a different user reports otherwise. |
| OQ-2 | What `type` enum values does Mono actually return? | `eAid`, `black`, `platinum`, `white` observed. Two `black` cards (USD + EUR). No `gold`/`iron`/`fop`/`yellow`/`platina` in this dataset. |
| OQ-3 | Does the rate gate hold under real load? | Yes — zero 429s in two consecutive real round-trips. Second call blocked the expected ~45s. |
| OQ-4 (new) | "First card by id" picks `eAid` — is that what we want? | **No.** Phase 2 should either skip `eAid`/inactive types or make the polled card configurable (e.g. `MONO_PRIMARY_ACCOUNT_ID` env var). For Phase 1 the workaround is the operator deleting unwanted accounts from the table. |

## What this enables

Phase 1 walking skeleton is shippable as a single `docker compose up`. Phase 2 (multi-account scheduling, backfill) inherits a working real-Mono integration, idempotent writes, and a clean log baseline. Phase 3 (FX rollups, categorization) inherits a populated `transactions` table with real `raw_payload` for every row.
