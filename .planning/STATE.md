---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 03
current_plan: 4
status: verifying
last_updated: "2026-05-30T15:04:39.554Z"
progress:
  total_phases: 7
  completed_phases: 3
  total_plans: 12
  completed_plans: 12
  percent: 43
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
Plan: 4 of 4

- **Current phase:** 03
- **Current plan:** 4
- **Status:** Phase complete — ready for verification
- **Progress:** Phase 2/7 complete

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

Executed Plan 03-03 (UAH rollup read path): new `services/fx_rollup.rollup()` (Decimal banker's-rounding UAH math, native_uah/mono_card/nbu labels, fx_stale, Decimal-as-string fx_rate — D-11..D-14, no double-conversion FX-04); rewrote `TransactionRepo.list_for_account` to the D-14 `LEFT JOIN LATERAL` carry-forward read returning enriched mapping rows (FX-03, computed on read); `MonobankImporter` now sets `attributed_day = time→Europe/Kyiv date` (D-09, frozen on upsert); added 5 FX fields to `TransactionOut` + route wiring. Flipped 4 plan-owned scaffolds (test_fx_rollup_math/join/on_card/stale_fallback) xfail→live PASS. Fixed a cross-test fx_rates contamination (Rule 1) with hermetic autouse truncate; added `.gitignore` for `__pycache__`. Full suite: 106 passed, 4 xfailed (Plan-04 scaffolds), 0 failed. Commits 49f96d2, 1c20e84, 1bb9a86.

### Next Action

Execute Plan 03-04 (FX cron/bootstrap lifecycle): wire `NbuFxImporter` + both repos into the scheduler — daily NBU fetch + lazy bootstrap on first-seen currency. Flips the remaining xfail scaffolds: test_fx_bootstrap_lazy, test_fx_tick (×3).

### Recovery

If context is lost, read in this order:

1. `.planning/PROJECT.md` (Core Value, constraints)
2. `.planning/REQUIREMENTS.md` (v1 REQ-IDs and traceability)
3. `.planning/ROADMAP.md` (phase structure and success criteria)
4. `.planning/STATE.md` (this file — current position)
5. `.planning/research/SUMMARY.md` (TL;DR of stack/architecture/pitfalls)

---
*State initialized: 2026-05-10 after roadmap creation*
