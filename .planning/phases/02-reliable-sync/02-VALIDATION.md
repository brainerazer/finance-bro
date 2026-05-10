---
phase: 02
slug: reliable-sync
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-10
---

# Phase 02 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `02-RESEARCH.md` § Validation Architecture (lines 1105-1166).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.x + pytest-asyncio 1.3 (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) — already configured by Phase 1 |
| **Quick run command** | `uv run pytest <focused-test-file> -x` |
| **Full suite command** | `uv run pytest -x` |
| **Estimated runtime** | ~5s focused per task; ~60–90s full suite (testcontainers Postgres warm) |

---

## Sampling Rate

- **After every task commit:** `uv run pytest <focused-test-file> -x`
- **After every plan wave:** `uv run pytest -x`
- **Before `/gsd-verify-work`:** Full suite green AND a real-Mono smoke (analogous to Phase 1's 01-04 manual verification): start the container with a real `MONO_TOKEN`, observe `/api/import/status` populates after ~10s, observe a `live` `import_runs` row completes within 65s, observe `GET /api/transactions` reflects new rows if any landed in the polling window.
- **Max feedback latency:** 5 seconds (focused), 90 seconds (full suite).

---

## Per-Task Verification Map

> Task IDs are placeholders (`{N}-{plan}-{task}`) until the planner emits `02-XX-PLAN.md`. The planner MUST update this table to point each task at one of the rows below.

| Req / SC / Decision | Behavior | Test Type | Automated Command | File Exists |
|---------------------|----------|-----------|-------------------|-------------|
| ING-05 | Hold transaction (`hold:true`) ingested with hold flag in DB and TransactionOut | unit | `uv run pytest tests/test_hold_cleared_upsert.py::test_hold_inserted_with_flag -x` | ❌ W0 |
| ING-05 | Cleared payload (`hold:false`) with same `(account_id, source_tx_id)` UPDATEs in place — single row, mutated `hold` / `amount_minor` / `raw_payload`, frozen `is_user_locked` / `category_*` | unit | `uv run pytest tests/test_hold_cleared_upsert.py::test_cleared_updates_in_place -x` | ❌ W0 |
| ING-05 | `TransactionOut.hold: bool` field present in `GET /api/transactions` response | unit | `uv run pytest tests/test_transactions_route.py::test_hold_field_in_response -x` | ❌ W0 (extends existing file) |
| ING-06 | 12 backfill rows enqueued newest-first on first tick after boot for fresh card | unit | `uv run pytest tests/test_backfill_enqueue.py::test_twelve_chunks_newest_first -x` | ❌ W0 |
| ING-06 | Killed mid-chunk: `in_flight` row swept back to `pending` on lifespan startup | integration | `uv run pytest tests/test_backfill_resumability.py::test_recover_in_flight_on_restart -x` | ❌ W0 |
| ING-06 | Backfill chunks walk newest-first, persist per-chunk completion, resume from where stopped | unit | `uv run pytest tests/test_backfill_resumability.py::test_resume_picks_remaining_chunks -x` | ❌ W0 |
| ING-06 | 30-day window math: 12 chunks × 30 days, all UTC seconds, no ms multiplication | unit | `uv run pytest tests/test_backfill_window_math.py -x` | ❌ W0 |
| ING-06 | 4xx response inside backfill chunk → `import_runs.status='error'`, not silent skip | unit | `uv run pytest tests/test_backfill_resumability.py::test_4xx_marks_error_not_skip -x` | ❌ W0 |
| ING-08 | `GET /api/import/status` returns the full D-14 shape | unit | `uv run pytest tests/test_import_status_shape.py::test_status_response_shape -x` | ❌ W0 |
| ING-08 | Mono 401 → `scheduler_state.state='auth_failed'`, persisted across simulated restart | integration | `uv run pytest tests/test_401_stops_scheduler.py::test_401_persists_across_restart -x` | ❌ W0 |
| ING-08 | Mono 429 → per-call `accounts[i].last_status='rate_limited'`, scheduler keeps running | unit | `uv run pytest tests/test_429_does_not_stop.py -x` | ❌ W0 |
| ING-08 | `accounts[].last_polled_at` reflects last successful `live` run | unit | `uv run pytest tests/test_import_status_shape.py::test_last_polled_at_per_account -x` | ❌ W0 |
| SC#1 | Round-robin across 3 active cards visits each within 3 ticks (mocked gate) | unit | `uv run pytest tests/test_scheduler_round_robin.py::test_three_cards_visited_three_ticks -x` | ❌ W0 |
| SC#1 / D-01 | Allowlist excludes eAid: tick never picks an `eAid` card | unit | `uv run pytest tests/test_scheduler_round_robin.py::test_eaid_skipped -x` | ❌ W0 |
| SC#2 | 12-month backfill on fresh install enqueues 12 chunks per card and consumes them across ticks | integration | `uv run pytest tests/test_backfill_resumability.py::test_full_12_month_walk -x` | ❌ W0 |
| SC#3 | Hold→cleared end-to-end: insert with `hold:true`, fixture re-fetch with `hold:false`, single row remains, fields per D-10 | integration | `uv run pytest tests/test_hold_cleared_upsert.py::test_e2e_hold_then_cleared -x` | ❌ W0 |
| SC#4 | Status surface distinguishes 401 (banner state) from 429 (transient) | integration | `uv run pytest tests/test_import_status_shape.py::test_401_vs_429_distinguished -x` | ❌ W0 |
| D-16 | `POST /api/import` returns 202 with `{enqueued: [{account_id, run_id}]}`, NOT a synchronous body | unit | `uv run pytest tests/test_force_poll_endpoint.py::test_returns_202_enqueued -x` | ❌ W0 (REPLACES synchronous-body assertions in `tests/test_import_route.py`) |
| Phase 1 invariant | Composite idempotency on `(account_id, source_tx_id) WHERE NOT is_deleted` still holds; `is_user_locked` not overwritten by upsert | unit | `uv run pytest tests/test_partial_unique_index.py tests/test_idempotency.py -x` | ✅ existing |
| Phase 1 invariant | RateLimitGate still owns the 65s cadence | unit | `uv run pytest tests/test_rate_limit_gate.py -x` | ✅ existing |
| Phase 1 invariant | Log redaction still hides token / X-Token / amount values at INFO+ | unit | `uv run pytest tests/test_log_redaction.py -x` | ✅ existing |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

New test files (planner MUST create these in Wave 0 before any production code lands):

- [ ] `tests/test_scheduler_round_robin.py` — covers SC#1 + D-01 / D-02 allowlist & order
- [ ] `tests/test_backfill_enqueue.py` — covers ING-06 enqueue logic (D-05, D-08)
- [ ] `tests/test_backfill_resumability.py` — covers ING-06 + SC#2 resume + 4xx-as-error + in-flight sweep
- [ ] `tests/test_backfill_window_math.py` — covers `MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30` arithmetic
- [ ] `tests/test_hold_cleared_upsert.py` — covers ING-05 + SC#3 + D-10 frozen-fields invariant
- [ ] `tests/test_import_status_shape.py` — covers ING-08 + SC#4 + D-14
- [ ] `tests/test_401_stops_scheduler.py` — covers ING-08 + SC#4 sticky-401 (D-15)
- [ ] `tests/test_429_does_not_stop.py` — covers ING-08 + D-15 transient-429
- [ ] `tests/test_force_poll_endpoint.py` — covers D-16 (replaces synchronous-body assertions in `test_import_route.py`)

Modifications to existing test files (Wave 0 in the same wave as the new files):

- [ ] **MODIFY** `tests/test_import_route.py` — remove synchronous-body assertions (`statement_count`, `inserted`, `skipped_duplicates`); replace with 202 + `{enqueued: [...]}` shape per D-16.
- [ ] **MODIFY** `tests/test_transactions_route.py` — add assertion that `TransactionOut.hold` is present and reflects the DB value.

New fixtures:

- [ ] `tests/fixtures/statement_with_hold.json` — Mono `statementItem` payload with `hold: true`
- [ ] `tests/fixtures/statement_cleared_followup.json` — same `id` as above with `hold: false` and possibly different amount

Dependency install:

- [ ] `uv add apscheduler==3.11.2` — only new top-level dep; without it, the `SchedulerRunner` module won't import. (Already pinned in `research/STACK.md` per CLAUDE.md TL;DR; this is the install action.)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real-Mono auto-poll smoke | SC#1 | Requires a live `MONO_TOKEN` and ≥1 hour of idle time; can't be CI-bound | Start container with real token; leave for ~1h; observe `/api/import/status` populates within 10s; new transactions appear in `GET /api/transactions` within ~3 min of posting |
| Real-Mono backfill smoke | SC#2 / ING-06 | Requires a live `MONO_TOKEN` against a real account with ≥12 months history; consumes ~13 min of polling slots | On a fresh install with real token, observe 12 `import_runs` rows enqueued per active card, watch them complete newest-first across ticks |
| Real-Mono 401 smoke | SC#4 / D-15 | Requires deliberately invalidating the token (e.g., revoking via Mono support or pasting a malformed token) | After 401 lands, restart container with the same bad token; confirm `scheduler_state.state='auth_failed'` is read at startup and scheduler does NOT re-flood Mono |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s focused / < 90s full
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
