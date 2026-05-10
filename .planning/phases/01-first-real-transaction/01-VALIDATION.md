---
phase: 1
slug: first-real-transaction
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-10
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Templated from `01-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.3 + pytest-asyncio 1.3.0 (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` (Wave 0 installs) |
| **Quick run command** | `uv run pytest tests/ -x --tb=short` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Coverage (optional)** | `uv run pytest --cov=src/finance_bro --cov-report=term-missing` |
| **Estimated runtime** | ~30–60 seconds (testcontainers Postgres warm-up dominates) |

Test isolation: real Postgres via `testcontainers-python`. SQLite is banned for tests because the production schema relies on JSONB, partial unique indexes, and `SELECT … FOR UPDATE` — SQLite would silently pass while production breaks.

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x --tb=short`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd-verify-work`:** Full suite must be green AND manual SC#1 / SC#2 / SC#5 checks pass
- **Max feedback latency:** ~60 seconds (testcontainers cold start)

---

## Per-Task Verification Map

> Task IDs follow the convention `{phase}-{plan}-{task}` and are populated by the planner. Until plans land, this table is keyed by Requirement ID.

| Req ID | Behavior | Test Type | Automated Command | File Exists | Status |
|--------|----------|-----------|-------------------|-------------|--------|
| ING-01 | `MonobankImporter.discover_accounts()` parses `client-info`; numeric → alpha currency mapping at boundary | unit | `uv run pytest tests/test_importer_currency_map.py -x` | ❌ Wave 0 | ⬜ pending |
| ING-01 | `MonobankImporter.fetch_statement()` produces `CanonicalTransaction` with verbatim `raw` payload | unit | `uv run pytest tests/test_importer_statement.py -x` | ❌ Wave 0 | ⬜ pending |
| ING-01 | Mono token rides in `X-Token` header — NEVER in URL | unit | `uv run pytest tests/test_importer_no_token_in_url.py -x` | ❌ Wave 0 | ⬜ pending |
| ING-02 | `RateLimitGate.acquire()` cannot fire twice within 60 s | unit | `uv run pytest tests/test_rate_limit_gate.py::test_within_window -x` | ❌ Wave 0 | ⬜ pending |
| ING-02 | `RateLimitGate` state persists across instance recreation (simulated restart) | unit | `uv run pytest tests/test_rate_limit_gate.py::test_persists_across_restart -x` | ❌ Wave 0 | ⬜ pending |
| ING-02 | Two concurrent `acquire()` callers serialize via `SELECT … FOR UPDATE` | unit | `uv run pytest tests/test_rate_limit_gate.py::test_concurrent_serialize -x` | ❌ Wave 0 | ⬜ pending |
| ING-03 | `POST /api/import` writes `raw_payload` JSONB verbatim | integration | `uv run pytest tests/test_import_route.py::test_raw_payload_verbatim -x` | ❌ Wave 0 | ⬜ pending |
| ING-03 | All Mono account kinds (card + jar + FOP) populate the `accounts` table on first import (D-05) | integration | `uv run pytest tests/test_import_route.py::test_all_accounts_persisted -x` | ❌ Wave 0 | ⬜ pending |
| ING-03 | `GET /api/transactions` returns rows with `amount_minor: int`, `currency: "XXX"`, full `raw_payload` (D-10) | integration | `uv run pytest tests/test_transactions_route.py -x` | ❌ Wave 0 | ⬜ pending |
| ING-04 | Two imports of the same Mono `id` produce one row (`inserted=N` then `inserted=0, skipped_duplicates=N`) | integration | `uv run pytest tests/test_idempotency.py -x` | ❌ Wave 0 | ⬜ pending |
| ING-04 | Soft-deleted row can be re-inserted (partial unique index respects `WHERE NOT is_deleted`) | unit | `uv run pytest tests/test_partial_unique_index.py -x` | ❌ Wave 0 | ⬜ pending |
| ING-07 | `is_deleted` column defaults to `false`; `raw_payload` is never mutated by application code | unit | `uv run pytest tests/test_schema_invariants.py -x` | ❌ Wave 0 | ⬜ pending |
| FX-01 | `amount_minor` stored as BIGINT signed minor units; currency stored as CHAR(3) ISO-4217 alpha | unit | `uv run pytest tests/test_money_invariants.py -x` | ❌ Wave 0 | ⬜ pending |
| FX-01 | Importer never produces `float`; round-trip `int → DB → JSON → int` is exact | unit | `uv run pytest tests/test_money_invariants.py::test_no_float_in_pipeline -x` | ❌ Wave 0 | ⬜ pending |
| OPS-01 | `MONO_TOKEN` is read once at startup from env; never written to DB or file | unit | `uv run pytest tests/test_settings.py::test_token_env_only -x` | ❌ Wave 0 | ⬜ pending |
| OPS-04 | Full import cycle produces zero token-shaped substrings in INFO log output | integration | `uv run pytest tests/test_log_redaction.py::test_no_token_in_logs -x` | ❌ Wave 0 | ⬜ pending |
| OPS-04 | Full import cycle produces zero `amount` values in INFO log output | integration | `uv run pytest tests/test_log_redaction.py::test_no_amounts_in_logs -x` | ❌ Wave 0 | ⬜ pending |
| OPS-04 | `X-Token` header value never appears in any log record | integration | `uv run pytest tests/test_log_redaction.py::test_no_x_token_header -x` | ❌ Wave 0 | ⬜ pending |
| DEP-01 | Migration `0001_walking_skeleton` round-trips: `upgrade head` → `downgrade base` → `upgrade head` clean | integration | `uv run pytest tests/test_migrations.py -x` | ❌ Wave 0 | ⬜ pending |
| DEP-01 | `docker compose config` validates without errors (smoke) | manual | `docker compose -f compose.yml config` | n/a — manual gate | ⬜ pending |
| DEP-02 | No auth middleware registered; `/docs` reachable without credentials | unit | `uv run pytest tests/test_no_auth.py -x` | ❌ Wave 0 | ⬜ pending |

*Status legend:* ⬜ pending · ✅ green · ❌ red · ⚠️ flaky

---

## Wave 0 Requirements

Every test file in the requirements map is currently absent (greenfield). Wave 0 must land before any Wave 1 task can run a green test:

- [ ] `uv add --dev pytest pytest-asyncio testcontainers respx asgi-lifespan freezegun` (install harness)
- [ ] `tests/conftest.py` — fixtures: `pg_url` (testcontainers Postgres), `session_factory`, `client` (httpx + asgi-lifespan), `respx_mock`
- [ ] `tests/__init__.py` (empty marker)
- [ ] `tests/fixtures/` directory with canned Mono responses:
  - [ ] `client_info_minimal.json` — one card + one jar
  - [ ] `statement_two_items.json` — two `statementItem` rows
- [ ] `pyproject.toml` `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`
- [ ] Document precondition: Docker daemon must be running (testcontainers requirement)
- [ ] Empty stub files for every test file in the map above so test discovery passes (one `def test_placeholder(): assert False, "Wave 0 stub"` per file is fine — they go red until implemented)

*If none: "Existing infrastructure covers all phase requirements." — does not apply, this is a greenfield phase.*

---

## Manual-Only Verifications

Cannot be automated under 30 seconds. Phase gate before `/gsd-verify-work`:

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `docker compose up` starts the app on the LAN; `/docs` opens; token-paste flow works | SC#1 / DEP-01 | Requires Docker host + browser — not in pytest scope | `docker compose up -d && curl -fsS http://localhost:8000/docs > /dev/null && echo OK` |
| Real Mono call via `POST /api/import` returns real card rows | SC#2 / ING-01 / ING-03 | Requires Bohdan's real `MONO_TOKEN`; cannot ship a synthetic test against the live API in CI | After `compose up`, paste real token, click "Import now", `curl http://localhost:8000/api/transactions \| jq '. \| length'` returns ≥ 1 |
| `docker logs` is clean of token / `X-Token` / `amount` substrings after a real import | SC#5 / OPS-04 | Requires real import + live container — `respx` mocks can't prove the redaction filter survives the production logging path | `docker logs $(docker compose ps -q app) 2>&1 \| grep -ciE "(token\|x-token\|amount[^_])" \| grep -q "^0$"` |

*If none: "All phase behaviors have automated verification." — does not apply.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify command or Wave 0 dependency listed
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all `❌ Wave 0` references in the per-task map
- [ ] No `--watch` / `-f` / live-reload flags in any pytest command
- [ ] Feedback latency < 60 s (testcontainers cold-start budget)
- [ ] `nyquist_compliant: true` set in frontmatter once plans wire task IDs to this map

**Approval:** pending
