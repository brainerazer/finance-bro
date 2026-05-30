# Phase 4: Categorized Spending - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-30
**Phase:** 04-categorized-spending
**Areas discussed:** Default taxonomy + MCC seed, Predicate model & rule shape, When rules auto-run, Run-over-history diff + scope

---

## Area 1 — Default taxonomy + MCC seed

### Taxonomy definition / editability

| Option | Description | Selected |
|--------|-------------|----------|
| Fixed seed, fully editable after | Migration seeds ~15 categories; rename/recolor/add/delete freely after | ✓ |
| Seed + 'Uncategorized' sentinel | Same seed plus a reserved non-deletable Uncategorized row | |
| Minimal seed, you build it | Tiny core only; user adds the rest | |

**User's choice:** Fixed seed, fully editable after (D-01)

### Uncategorized representation

| Option | Description | Selected |
|--------|-------------|----------|
| NULL + explicit flag | category_id NULL + is_categorized:false boolean in DTO | |
| NULL only | category_id NULL; client infers uncategorized | ✓ |

**User's choice:** NULL only (D-02)

### MCC seed ambition

| Option | Description | Selected |
|--------|-------------|----------|
| Curated MCC-group seed rules | Pre-seeded editable RULES for common MCC ranges | ✓ |
| Broad MCC table, fewer gaps | Exhaustive MCC→category map | |
| Minimal seed rules | Only highest-confidence mappings | |

**User's choice:** Curated MCC-group seed rules (D-04) — MCC defaults are pre-seeded rules, not hardcoded, so the rules engine is the single categorization mechanism.

---

## Area 2 — Predicate model & rule shape

### Matching ops

| Option | Description | Selected |
|--------|-------------|----------|
| Substring/equality only (no regex) | Fixed op vocab, no regex — eliminates ReDoS | ✓ |
| Substring + safe-regex (RE2-style) | Linear-time regex engine | |
| Substring + Python re with guards | Python re + length/timeout guards | |

**User's choice:** Substring/equality only (D-05). Consciously narrows CAT-01's "regex" wording; regex deferred to a future RE2 op.

### Predicate combination

| Option | Description | Selected |
|--------|-------------|----------|
| Flat AND-only | List of conditions, all must match; OR via two rules | ✓ |
| AND with IN-lists (one-level OR) | (effectively the same — IN covers set membership) | |
| Nested AND/OR tree | Full boolean tree | |

**User's choice:** Flat AND-only, IN-lists cover the common OR case (D-06)

### Rule action

| Option | Description | Selected |
|--------|-------------|----------|
| Category only | Sets category_id + category_source='rule' | ✓ |
| Category + optional note | Also sets a description/note override | |

**User's choice:** Category only (D-07)

### Lock semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Manual edit locks; rules skip locked | manual → source='manual'+locked; rule rows stay re-evaluable | ✓ |
| Manual edit locks; rules skip any categorized | rule runs only touch NULL-category rows | |

**User's choice:** Manual edit locks; rules skip locked unconditionally; rule-categorized rows remain re-evaluable (D-09)

---

## Area 3 — When rules auto-run

### Auto-run trigger

| Option | Description | Selected |
|--------|-------------|----------|
| On import, new/unlocked rows only | Categorize newly-touched non-locked rows each import tick | ✓ |
| Explicit-only | Never auto-run | |
| On import + on rule create/edit | Auto-apply new/edited rules to history with no preview | |

**User's choice:** On import, new/unlocked rows only; rule create/edit goes through CAT-05 preview, not auto-apply (D-10)

### Integration point

| Option | Description | Selected |
|--------|-------------|----------|
| Service step after insert_many | import_service calls categorizer after insert; repo stays pure | ✓ |
| Inside insert_many | Fold categorization into the repo upsert | |
| Separate post-import tick | Second scheduler job sweeps uncategorized | |

**User's choice:** Service step after insert_many — engine reusable by the history sweep, sets up the Categorizer seam (D-11)

---

## Area 4 — Run-over-history diff + scope

### Diff preview shape

| Option | Description | Selected |
|--------|-------------|----------|
| Counts + per-row changes | Summary counts + per-row old→new + skipped-locked count | ✓ |
| Counts only | Summary numbers only | |
| Counts + capped sample | Counts + first-N sample | |

**User's choice:** Counts + per-row changes (D-12)

### Preview→commit handshake

| Option | Description | Selected |
|--------|-------------|----------|
| Stateless re-run + token | Re-run on commit, staleness token guards drift | ✓ |
| Stateless re-run, no guard | Re-run on commit, no staleness check | |
| Persisted diff + apply-by-id | Store the diff job, apply exact set by id | |

**User's choice:** Stateless re-run + staleness token (D-13)

### Overwrite scope

| Option | Description | Selected |
|--------|-------------|----------|
| All non-locked (overwrite rule rows) | Re-evaluate every non-locked row; locked skipped | ✓ |
| Uncategorized-only by default, overwrite opt-in | Default fills NULL only; overwrite behind a flag | |

**User's choice:** All non-locked rows re-evaluated; locked always skipped (D-14)

### Category delete integrity

| Option | Description | Selected |
|--------|-------------|----------|
| Block if referenced (RESTRICT) | 409 listing references; reassign first | ✓ |
| Cascade to uncategorized | Null transactions, delete/disable rules | |
| Soft-delete (archive) | is_archived flag, no hard delete | |

**User's choice:** Block if referenced — ON DELETE RESTRICT + pre-check with clear 409 (D-15)

---

## Claude's Discretion

- Exact final category list/names/colors and precise MCC ranges per seed rule (Ukrainian-context groupings).
- Rule/category table columns, indexes, and the predicate JSON field schema (must encode the closed op vocabulary).
- Endpoint paths/verbs beyond those named; rule-list pagination; how priority is represented (order column vs reorder endpoint).
- Staleness-token hashing scheme and matched-row-state capture.
- Categorizer engine as pure module vs small class — as long as it's shared by the import step and history sweep.

## Deferred Ideas

- Regex predicates (future RE2 op) — narrowed from CAT-01 per Pitfall 8.
- Nested AND/OR predicate trees.
- Capped/sampled diff payloads.
- V2-CAT-01 auto-rule suggestion; V2-CAT-02 LLM categorizer via the Categorizer port.
- UI-03 quick re-categorize from feed; UI-04 detail drawer with matched rule — Phase 6.
