---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 03
current_plan: 3
status: executing
last_updated: "2026-05-30T17:40:00.000Z"
progress:
  total_phases: 7
  completed_phases: 2
  total_plans: 12
  completed_plans: 10
  percent: 33
---

# State: finance-bro

**Last updated:** 2026-05-10

## Project Reference

- **Name:** finance-bro
- **Mode:** mvp
- **Granularity:** standard
- **Core Value:** Automatic visibility into where my money goes — zero manual upkeep, on hardware I own.
- **PROJECT.md:** `.planning/PROJECT.md`
- **REQUIREMENTS.md:** `.planning/REQUIREMENTS.md`
- **ROADMAP.md:** `.planning/ROADMAP.md`
- **Research:** `.planning/research/` (SUMMARY.md, ARCHITECTURE.md, STACK.md, FEATURES.md, PITFALLS.md)

## Current Position

Phase: 03 (uah-truth) — EXECUTING
Plan: 3 of 4

- **Current phase:** 03
- **Current plan:** 1
- **Status:** Executing Phase 03
- **Progress:** Phase 0/7 complete

```
[x] [x] [~] [ ] [ ] [ ] [ ]
 1   2   3   4   5   6   7
```

## Performance Metrics

| Metric | Value |
|--------|-------|
| Phases planned | 0/7 |
| Phases complete | 0/7 |
| Plans complete | 0 |
| v1 requirements mapped | 35/35 |
| v1 requirements complete | 0/35 |

## Accumulated Context

### Decisions Made

- **Stack:** FastAPI 0.136 + SQLAlchemy 2.0 + Postgres 17 (compose); React 19 + Vite 8 + TanStack Query 5 (frontend) — see `research/STACK.md`
- **Database:** Postgres 17 in compose, NOT SQLite — homelab/NFS reality forces this; see `research/SUMMARY.md` Conflict 1
- **Mono timestamp model:** only `time` (Unix UTC); derive `attributed_day` via `zoneinfo.ZoneInfo("Europe/Kyiv")`; no `operationDate` field exists
- **Money representation:** `BIGINT` minor units + ISO-4217 alpha column in DB, `Decimal` in Python, never `float`
- **Idempotency key:** composite `(account_id, source_tx_id)` — Mono `id` is per-account scope, not globally unique
- **Rate-limit gate:** single token bucket (1 req/60s) persisted to disk, owned by `MonobankImporter`
- **UAH rollup:** computed on read via `transactions × fx_rates`; never denormalized into a stored column
- **No app-level auth:** Tailscale/LAN is the trust boundary
- **No webhooks in v1:** polling only
- **NBU FX importer (03-02):** `NbuFxImporter.fetch_range` is a single-call range fetch over `exchange_site`, no token/no gate (NBU is unauthenticated); rates parsed via `json.loads(resp.text, parse_float=Decimal)` — never `resp.json()` (Pitfall 1); empty body → `[]` (D-16); tenacity retry capped 3x / 30s
- **FX persistence (03-02):** `FxRateRepo.upsert_many` is ON CONFLICT (rate_date,currency) DO NOTHING (idempotent); `TrackedFxCurrencyRepo.mark_attempted` records `last_error` only and never touches `scheduler_state` (D-08 — FX failures isolated from the Mono poll cursor)

### Open Questions

These are flagged in research and to be resolved during implementation, not now:

- Mono `statementItem.id` global vs per-account uniqueness — resolve empirically in Phase 1/2
- NBU weekend/holiday API response shape — validate against a real Sunday fixture in Phase 3
- Mono historical retention horizon (12mo vs longer) — observe in Phase 2 backfill
- FOP token: same personal token or separate? — verify in Phase 1 against real `client-info` response
- Mono 429 response: includes `Retry-After`? — observe in Phase 1/2 controlled test

### TODOs (Discovered During Planning)

- Test restore procedure manually before declaring v1 done (Phase 7 success criterion)
- Confirm whether Bohdan has a FOP account (affects Phase 1/2 multi-token branching)

### Blockers

None.

## Session Continuity

### Last Action

Executed Plan 03-02 (FX ingestion slice): added `FxRatesPort`/`FxRateRow` + `NbuFxImporter` (single-call NBU range fetch, Decimal rates, empty→[]), extended `currency_map` (PLN/GBP/CHF), and built `FxRateRepo` (idempotent upsert + count) + `TrackedFxCurrencyRepo` (ordered iterate/first-seen upsert/bootstrap flag/last-error). NbuFxImporter scaffolds flipped xfail→live PASS; added `test_fx_repos.py` for live repo coverage. Full suite: 70 passed, 4 xfailed (Plan 03/04 scaffolds), 1 skipped, 0 failed. Commits 35c5c43, 2e9a4d2.

### Next Action

Execute Plan 03-03 (UAH rollup read-path: LATERAL carry-forward join, fx_on_card multi-currency convert, banker's-rounding rollup math — flips test_fx_rollup_join / test_fx_on_card). Plan 03-04 then wires the cron/bootstrap lifecycle (flips test_fx_bootstrap_lazy / test_fx_stale_fallback / test_fx_tick).

### Recovery

If context is lost, read in this order:

1. `.planning/PROJECT.md` (Core Value, constraints)
2. `.planning/REQUIREMENTS.md` (v1 REQ-IDs and traceability)
3. `.planning/ROADMAP.md` (phase structure and success criteria)
4. `.planning/STATE.md` (this file — current position)
5. `.planning/research/SUMMARY.md` (TL;DR of stack/architecture/pitfalls)

---
*State initialized: 2026-05-10 after roadmap creation*
