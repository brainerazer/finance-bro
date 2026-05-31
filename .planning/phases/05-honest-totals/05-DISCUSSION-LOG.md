# Phase 5: Honest Totals - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-31
**Phase:** 5-honest-totals
**Areas discussed:** Cross-currency transfers, Refund matching depth, Confidence & auto-pair model, Reversal & re-run memory (+ Netting boundary)

---

## Cross-currency transfers

### Q1: How to decide two different-currency legs are the same transfer?
| Option | Description | Selected |
|--------|-------------|----------|
| FX-normalize to UAH | Compare UAH-rolled amounts; catches more, but FX-rounding tolerance needed | |
| Same-currency exact only | Auto-pair only same-currency exact `amount_minor`; mixed surfaces as candidate | ✓ |
| You decide | Planner picks after verifying Mono jar-transfer payloads | |

### Q2: How to detect a mixed-currency *candidate* (won't auto-pair)?
| Option | Description | Selected |
|--------|-------------|----------|
| FX-normalize w/ loose tolerance | Roll to UAH, flag candidate within small % tolerance + other gates | ✓ |
| Don't detect mixed-currency at all | Not reconciled in v1 | |
| You decide | Planner picks after data check | |

**User's choice:** Same-currency exact for auto-pair; mixed-currency surfaces as FX-normalized (loose-tolerance) candidate only.
**Notes:** Avoids FX-rounding false positives on the auto path while still never silently hiding a mixed-currency move.

---

## Refund matching depth

### Q1: How deep should refund pairing go in v1?
| Option | Description | Selected |
|--------|-------------|----------|
| Full-amount refunds only | Exact-equal refund nets to zero; partials stay normal | |
| Full + partial (single refund) | Partial nets to remainder; one refund per charge | ✓ |
| Full + partial + split | Many-to-one refunds; most complex | |

### Q2: Which charge does a partial refund attach to?
| Option | Description | Selected |
|--------|-------------|----------|
| Smallest charge ≥ refund, most recent first | Best amount-fit; avoids attaching to huge unrelated charge | ✓ |
| Most recent matching charge | Simpler; can mis-attach | |
| You decide | Planner picks | |

### Q3: Should partial refunds auto-pair or only surface as candidates?
| Option | Description | Selected |
|--------|-------------|----------|
| Partial → candidate only; full → auto-pair | Conservative; eyeball partials | |
| Partial can auto-pair too | Same gate set as full | ✓ |

**User's choice:** Full + partial single refund; attach to smallest unrefunded charge ≥ refund (counterparty/MCC, ±60d), tie-break most recent; partials auto-pair on the same gate set.
**Notes:** Split/multi-refund deferred to a future iteration.

---

## Confidence & auto-pair model

### Q1: Signal count vs weighted score?
| Option | Description | Selected |
|--------|-------------|----------|
| Signal-count buckets | Count matching signals; ≥3 auto, 2 candidate, ≤1 ignore | ✓ |
| Weighted score | Per-signal weights summed to 0–1 | |
| You decide | Planner picks after data check | |

### Q2: Which signals are hard gates before an auto-pair? (multi-select)
| Option | Description | Selected |
|--------|-------------|----------|
| Opposite sign | One outflow, one inflow | ✓ |
| Amount match (exact, same currency) | Transfers exact; refunds refund ≤ charge | ✓ |
| Time window (±2d / ±60d) | Within respective window | ✓ |
| Both accounts user-owned / counterparty-MCC overlap | Ownership (transfer) / identity (refund) | ✓ |

### Q3 (follow-up): What produces a candidate given all four are gates? (multi-select)
| Option | Description | Selected |
|--------|-------------|----------|
| Amount: FX-approximate instead of exact | Mixed-currency within tolerance | ✓ |
| Identity: MCC overlaps but counterparty differs | Refund with weaker identity match | ✓ |
| Time: just outside tight window, inside outer band | Wider grace band | ✓ |
| None — only area-1 FX case | Tightest; everything else auto or ignored | |

**User's choice:** Signal-count buckets; all four signals are hard gates for auto-pair; a soft-miss of exactly one gate (FX-approx amount, MCC-but-not-counterparty, or just-outside-time) → candidate; two+ soft-misses → ignored.
**Notes:** All four selected as hard gates surfaced a tension (the pending path would vanish), resolved via the soft-miss-of-one-gate candidate model.

---

## Reversal & re-run memory

### Q1: How to remember an unlinked/rejected pair?
| Option | Description | Selected |
|--------|-------------|----------|
| Tombstone keyed on the tx pair | Ordered (tx_a, tx_b) + signal fingerprint; recon skips live tombstones | ✓ |
| Status flag on the link row | Soft-delete instead of hard delete | |
| You decide | Planner picks | |

### Q2: What counts as a fresh signal to override a tombstone?
| Option | Description | Selected |
|--------|-------------|----------|
| Underlying tx changed since unlink | amount/hold/time change → fingerprint mismatch → may re-surface | ✓ |
| Never re-surface automatically | Permanent tombstone; manual re-link only | |
| You decide | Planner defines fingerprint rule | |

### Q3 (follow-up): Re-surface as auto-pair or candidate?
| Option | Description | Selected |
|--------|-------------|----------|
| Always re-surface as candidate | Respects prior rejection; needs re-confirmation | ✓ |
| Auto-pair if it clears all gates | Could silently return a pair you unlinked | |

**User's choice:** Tombstone keyed on pair + signal fingerprint; fresh signal = an underlying leg's amount/hold/time changed; a re-surfaced pair always returns as a pending candidate, never a silent re-pair.

---

## Netting boundary (closing question)

### Q: How does this phase expose netting for Phase 6 totals?
| Option | Description | Selected |
|--------|-------------|----------|
| Links table joined at query time | Compute-on-read helper/view; nothing denormalized | ✓ |
| Derived flag column on transactions | `excluded_from_spend` / `net_amount_minor` written per row | |
| You decide | Planner picks | |

**User's choice:** Links table joined at query time (compute-on-read), consistent with Phase 3 FX convention; Phase 6 calls a reusable helper/view.

---

## Claude's Discretion

- Links / tombstone schema details (table names, link-type & status enums, ordered-pair-key + fingerprint storage, indexes) — researcher verifies against real Mono jar-transfer/refund payloads first.
- Outer grace-band numbers for the time soft-miss (D-08).
- FX tolerance percentage for mixed-currency candidate detection (D-02).
- Reconciliation trigger/scope (on-import incremental pass vs manual history-sweep endpoint; whether a sweep needs a preview/commit + staleness-token handshake like `rules_history.py`).
- `GET /api/links/pending` / link DTO shapes and the candidate-confirm endpoint.
- Engine form (pure module vs `Reconciler` class), as long as it's reused by both the import-time pass and any history sweep.

## Deferred Ideas

- Cross-currency AUTO-pairing (v1 = candidates only).
- Split / one-charge-many-refunds (many-to-one) matching.
- Weighted confidence scoring.
- Smart "looks like a transfer" prompt for low-confidence pairs (V2-REC-01).
- Manual merge/split/cash entry + visible spending UI (Phase 6).
