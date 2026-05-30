---
phase: 4
slug: categorized-spending
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-30
---

# Phase 4 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9 + pytest-asyncio 1.3 (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` (`filterwarnings=["error"]` active — unclosed clients/resources hard-fail) |
| **Quick run command** | `uv run pytest tests/test_categorizer_interpreter.py -x -q` (pure unit, no Postgres) |
| **Full suite command** | `uv run pytest -q` (spins testcontainers Postgres via `conftest.py`) |
| **Estimated runtime** | ~60 seconds (full suite incl. container) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_categorizer_interpreter.py -x -q` (sub-second, no container)
- **After every plan wave:** Run `uv run pytest -q` (full suite incl. testcontainers Postgres + migration + integration)
- **Before `/gsd:verify-work`:** Full suite must be green; the CAT-04 lock-invariant test and the CAT-05 stale-token test are the two must-pass headline cases.
- **Max feedback latency:** ~60 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| W0 | — | 0 | CAT-01 | T-4-eval | Predicate interpreter is a closed-op `match`; no `eval`/`exec`/`re` reachable | unit | `uv run pytest tests/test_no_eval_in_categorizer.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-01 | — | Each op (icontains/equals/in_int/in_str/amount_sign/amount_range/hold) matches correctly; canonical ATB example matches | unit | `uv run pytest tests/test_categorizer_interpreter.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-01 | T-4-payload | Absent `raw_payload` counterparty key → no-match, never KeyError | unit | `uv run pytest tests/test_field_resolver.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-02 | — | First-match-wins; deterministic `(priority, id)`; equal-priority forbidden | unit | `uv run pytest tests/test_engine_first_match.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-03 | — | Migration 0004 seeds ~15 categories + MCC rules; FK present; downgrade clean | integration | `uv run pytest tests/test_migration_0004.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-03 | — | Category + rule CRUD round-trips; priority reorder | integration | `uv run pytest tests/test_categories_crud.py tests/test_rules_crud.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-04 | T-4-lock | Locked row untouched by import-step AND history sweep; manual recategorize sets both flags | integration | `uv run pytest tests/test_lock_invariant.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-05 | T-4-stale | Preview returns changed/overwritten/skipped counts + per-row diff + token; commit applies on match, 409 on stale | integration | `uv run pytest tests/test_history_preview_commit.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-01/D-11 | — | Import-step categorizes touched non-locked rows; same engine reused verbatim by history sweep | integration | `uv run pytest tests/test_categorize_on_import.py -x` | ❌ W0 | ⬜ pending |
| W0 | — | 0 | CAT-03/D-15 | T-4-fk | DELETE category referenced by rule/tx → 409 with counts; FK RESTRICT backstop | integration | `uv run pytest tests/test_category_delete_guard.py -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*Task IDs are W0 placeholders — the planner will assign concrete `{plan}-{task}` IDs and map each test file to the task that makes it green.*

---

## Wave 0 Requirements

- [ ] `tests/test_categorizer_interpreter.py` — per-op truth table (CAT-01), pure, no fixtures
- [ ] `tests/test_no_eval_in_categorizer.py` — static guard asserting no `eval`/`exec`/`re` import in `categorizer/`
- [ ] `tests/test_field_resolver.py` — column vs `raw_payload` resolution + absent-key safety (D-08)
- [ ] `tests/test_engine_first_match.py` — first-match-wins + lock skip (CAT-02/D-09)
- [ ] `tests/test_migration_0004.py` — seed counts, FK existence, downgrade
- [ ] `tests/test_categories_crud.py` / `tests/test_rules_crud.py` — CRUD + priority reorder
- [ ] `tests/test_lock_invariant.py` — headline CAT-04 test (lock → run history → unchanged)
- [ ] `tests/test_history_preview_commit.py` — preview shape + token + 409-on-stale (CAT-05/D-13)
- [ ] `tests/test_categorize_on_import.py` — import-step categorizes touched non-locked rows (D-10/D-11)
- [ ] `tests/test_category_delete_guard.py` — 409 with reference counts (D-15)
- [ ] Shared fixtures: `make_row(...)` helper for `RowView` construction in pure tests; reuse existing `session_factory`/`client` conftest fixtures for integration
- [ ] Framework install: none — pytest/pytest-asyncio/testcontainers already present

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Default taxonomy naming/colors "feel right" for Ukrainian context | CAT-03 | Subjective taste call (Claude's Discretion); correctness is covered by migration seed-count test | Eyeball the seeded `categories` rows after `0004` runs |

*All security-critical and contract behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
