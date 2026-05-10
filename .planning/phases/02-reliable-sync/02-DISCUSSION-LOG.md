# Phase 2: Reliable Sync - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-10
**Phase:** 02-reliable-sync
**Areas discussed:** Polling scope & round-robin, Backfill orchestration, Hold → cleared upsert semantics, Sync status surface

---

## Polling scope & round-robin

### Q1: Which account types should join the poll rotation by default?

| Option | Description | Selected |
|--------|-------------|----------|
| Cards + jars (Recommended) | Excludes eAid; cycle ~5–7 min; jars rarely have new tx so SC#1 still met for active cards | |
| Cards only (active types) | `mono.card` with type ∈ {black, platinum, white}; smallest rotation, ~4 min cycle for 4 cards | ✓ |
| Everything Mono returns | Cards + jars + FOPs + eAid; cycle blows out, eAid eats budget | |
| User-pinned subset (.env) | `MONO_POLL_ACCOUNT_IDS` env var explicit list | |

**User's choice:** Cards only (active types).
**Notes:** User chose tighter scope than recommended — jars are deferred. Pairs naturally with the type-allowlist follow-up below.

### Q2: What ordering policy should the round-robin use?

| Option | Description | Selected |
|--------|-------------|----------|
| By id ascending (Recommended) | Deterministic, no extra state, new accounts join at tail | ✓ |
| Activity-weighted | Recently-active cards polled more often; needs `last_tx_at` + weighting heuristic | |
| Round-robin with skip-on-empty | After N consecutive empty polls, double the interval; self-tunes eAid problem | |

**User's choice:** By id ascending.
**Notes:** Simplest path; allowlist already handles the eAid case so skip-on-empty isn't needed.

### Q3: How should the scheduler start and stop?

| Option | Description | Selected |
|--------|-------------|----------|
| Auto-start on app boot (Recommended) | FastAPI lifespan starts APScheduler; stops only on 401 or shutdown | ✓ |
| Manual start via POST /api/sync/start | Scheduler dormant on boot until endpoint hit; defeats zero-upkeep value | |
| Auto-start, 401 pauses but auto-retries hourly | Hourly probe of `/personal/client-info` to detect token recovery | |

**User's choice:** Auto-start on app boot.
**Notes:** Hourly auto-retry rejected because env-only token (Phase 1 D-01) can't recover without container restart anyway.

### Q4: How to filter "active" cards — `source_kind` only or explicit Mono `type` allowlist?

| Option | Description | Selected |
|--------|-------------|----------|
| type ∈ {black, platinum, white} allowlist (Recommended) | Persist Mono `type` on accounts, filter polling; eAid auto-skipped; fail-closed on new types | ✓ |
| source_kind == mono.card (any type) | Poll every card row regardless of type; eAid still polls | |
| Allowlist + manual override env var | type allowlist by default + `MONO_POLL_INCLUDE_TYPES` env var for extension | |

**User's choice:** Allowlist only.
**Notes:** No env-var override in v1; if a new card type is needed, that's a code change + redeploy.

### Q5: What does the scheduler do with persisted FOP/jar accounts?

| Option | Description | Selected |
|--------|-------------|----------|
| Persist, don't poll, surface in /api/accounts (Recommended) | Phase 1 D-05 stays; scheduler ignores them; future phase can poll without re-discovery | ✓ |
| Persist + poll opportunistically when budget free | Low-priority queue; rarely runs at single-user scale | |
| Don't persist non-polled types at all | Filter discovery itself; breaks D-05 invariant | |

**User's choice:** Persist, don't poll, surface in /api/accounts.

### Q6: Inactive-card defense — allowlist only or also "no tx in last 90 days" auto-skip?

| Option | Description | Selected |
|--------|-------------|----------|
| Allowlist is enough (Recommended) | Trust {black, platinum, white} filter; dormant card polling is cheap | ✓ |
| Allowlist + skip-after-N-empty | Track consecutive empty polls; after 30, back off to once an hour | |

**User's choice:** Allowlist is enough.

---

## Backfill orchestration

### Q1: How should the 12-month backfill be triggered?

| Option | Description | Selected |
|--------|-------------|----------|
| Auto on first scheduler tick (Recommended) | Detect "no history", enqueue 12 chunks before live polling resumes; zero touch | ✓ |
| Manual POST /api/backfill | Bohdan triggers explicitly; risks forgotten on first install | |
| Auto on first /api/import/status hit | Lazy via status endpoint; awkward coupling | |

**User's choice:** Auto on first scheduler tick.

### Q2: How does backfill share the rate gate with normal polling?

| Option | Description | Selected |
|--------|-------------|----------|
| Backfill pauses normal polling until done (Recommended) | Per-account: live polls skipped while backfill rows pending; gate still serializes globally | ✓ |
| Interleaved: backfill + polling share the gate fairly | Today's tx visible during backfill but doubles wall-clock time | |
| Backfill runs only at night / quiet hours | Cron-style; over-engineered for a one-time initial run | |

**User's choice:** Backfill pauses normal polling until done.
**Notes:** Per-account scoping (other cards keep polling normally) — captured in D-06.

### Q3: Backfill execution model — foreground HTTP or background job?

| Option | Description | Selected |
|--------|-------------|----------|
| Background, status via /api/import/status (Recommended) | APScheduler one-shot tasks; manual trigger returns 202 + run_id | ✓ |
| Foreground sync HTTP (block until done) | Blocks ~13 min per account; reverse-proxy timeouts a problem | |

**User's choice:** Background.

### Q4: What does the cursor model look like in `import_runs`?

| Option | Description | Selected |
|--------|-------------|----------|
| Per-account, per-window-chunk row (Recommended) | Full schema with status / error / attempts / window bounds; rich audit trail | ✓ |
| Single 'last_cursor' column on `accounts` | Smaller schema; loses per-chunk audit history | |
| Per-account row + per-chunk error_log table | Cursor + sidecar errors; more tables, more joins | |

**User's choice:** Per-account, per-window-chunk row.

---

## Hold → cleared upsert semantics

### Q1: Which fields mutate on the existing row when `(account_id, source_tx_id)` returns with `hold=false`?

| Option | Description | Selected |
|--------|-------------|----------|
| hold, amount_minor, raw_payload (Recommended) | Only fields Mono is allowed to change; manual edits never overwritten | ✓ |
| Everything from importer except user fields | Replaces hold/amount/currency/time/description/mcc/raw_payload; risks Phase 3 FX assumptions | |
| Replace via soft-delete + new row | Defeats SC#3 "same row updates in place" | |

**User's choice:** hold, amount_minor, raw_payload only.

### Q2: What happens to the original hold `raw_payload` when the cleared payload arrives?

| Option | Description | Selected |
|--------|-------------|----------|
| Overwrite — only latest payload kept (Recommended) | Simplest; audit trail in `import_runs` + `hold` flag itself | ✓ |
| Append: store as JSONB array of payloads | Preserves history; breaks Phase 1 API contract (`raw_payload: dict`) | |
| Separate `transaction_raw_payload_history` table | Most complete audit; v2 concern | |

**User's choice:** Overwrite.

### Q3: How does the API surface holds vs cleared transactions?

| Option | Description | Selected |
|--------|-------------|----------|
| Add `hold: bool` to TransactionOut, return all by default (Recommended) | Holds visible in feed (SC#3); Phase 6 dashboard does totals exclusion | ✓ |
| Default-exclude holds (`?include_holds=true` opt in) | Stricter "totals don't lie" but feed needs holds visible per SC#3 | |
| Separate `/api/transactions/pending` endpoint | Two endpoints; doubles surface; Phase 6 has to merge | |

**User's choice:** Add `hold: bool`, return all by default.

### Q4: Should we record the hold→cleared delta for debugging?

| Option | Description | Selected |
|--------|-------------|----------|
| No — import_runs covers it (Recommended) | Don't add columns chasing hypothetical debug needs | ✓ |
| Add `prior_amount_minor` column on transactions | Cheap; lets future UI show tip delta on cleared row | |

**User's choice:** No — import_runs covers it.

---

## Sync status surface

### Q1: What's the shape of `GET /api/import/status`?

| Option | Description | Selected |
|--------|-------------|----------|
| Top-level scheduler state + per-account last-poll table (Recommended) | `{scheduler, accounts[], backfill}`; renders directly in Phase 6 UI | ✓ |
| Top-level only | Cheaper but loses per-account visibility | |
| Top-level + 'recent runs' tail (last 20) | Per-account replaced with import_runs feed; worse for "is each card current?" | |

**User's choice:** Top-level + per-account.

### Q2: How are 401 (token revoked) and 429 (rate-limit) distinguished and handled?

| Option | Description | Selected |
|--------|-------------|----------|
| 401 → auth_failed sticky stop; 429 → backed_off, retry next slot (Recommended) | 401 stops scheduler until restart (token only recovers via .env edit); 429 handled by gate | ✓ |
| 401 → stop + auto-retry hourly | Hourly /personal/client-info probe; moot under env-only token | |
| Both 401 and 429 → exponential backoff, never stop | Treats 401 as transient; floods Mono with bad-auth | |

**User's choice:** 401 → auth_failed sticky; 429 → backed_off.

### Q3: Phase 1's `POST /api/import` — keep, change, or remove?

| Option | Description | Selected |
|--------|-------------|----------|
| Keep as 'force poll now' across all active cards (Recommended) | Returns 202 + enqueued list; useful "I just bought a coffee" UX | ✓ |
| Remove it — scheduler is the only path | Smaller API; loses force-poll UX | |
| Repurpose to `POST /api/backfill` | Confusing — same path with different semantics | |

**User's choice:** Keep as force-poll all active cards.
**Notes:** Phase 1's synchronous-blocking semantics are gone; the endpoint becomes async-enqueue (202).

### Q4: Error history retention — how much do we expose?

| Option | Description | Selected |
|--------|-------------|----------|
| Last-error per account + last-error per scheduler (Recommended) | Two error fields, latest only; full history via psql on import_runs | ✓ |
| Rolling last-N errors per account | Last 5 errors in JSON; bloats payload | |
| Just scheduler-level last_error | One global field; loses "which card failed" info | |

**User's choice:** Last-error per account + per scheduler only.

---

## Claude's Discretion

User did not select these framings; Claude's call within established framing:

- **Scheduler tick interval** — 10s (gate is the actual rate-limiter at 65s)
- **`mono_type` extraction location** — at `MonobankImporter.discover_accounts` boundary (where currency mapping already lives)
- **`accounts.mono_type` column migration** — single Alembic revision; backfill from `raw_payload->>'type'` in same migration
- **`import_runs` migration** — separate revision adding the table; no seeded data
- **APScheduler job structure** — single `tick()` job handling both enqueue + execute logic
- **`scheduler_state` persistence** — one-row table so `auth_failed` survives restart
- **Hold `description`/`mcc` first-insert population** — leave NULL in Phase 2; Phase 3 owns timezone/attribution
- **`POST /api/backfill` body shape** — `{account_id?, months?=12}`, both optional
- **No discovery-refresh endpoint in v1** — restart re-discovers via lazy first-import path
- **Test list** — six new test files covering round-robin, backfill resumability, hold/cleared upsert, status shape, 401 handling, force-poll endpoint

## Deferred Ideas

- Activity-weighted polling
- Skip-after-N-empty per-account backoff
- Manual `POST /api/accounts/refresh`
- Manual scheduler start/stop endpoints
- 401 auto-retry hourly (moot under env-only token)
- Per-call `tenacity` retries for transient errors
- `prior_amount_minor` audit column
- Hold-history JSONB array
- Separate `/api/transactions/pending` endpoint
- Top-N error feed in status response
- Periodic `client-info` re-discovery
- APScheduler v4 migration
