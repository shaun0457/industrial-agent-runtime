# B2.1 request-bound reservation handoff

Branch `feat/request-bound-reservation-v0`, base `main` `2f243bd`.
Owning spec: `docs/specs/deterministic-gates-v0.md`, section "B2.1 — Request-bound
budget reservation (D-048)". Resolves C4 SC-5; D-037 is unchanged.

## Semantics

A request-bound reservation is a deterministic amount, derived only from the
validated request and ToolSpec, that soundly upper-bounds the use of any conforming
execution of that request. It is exact only as the amount G2 admits and reserves,
not as a prediction: requested horizon 600 → reservation 600 → a successful run may
use 450, which is what gets charged. Normal conforming execution must not exceed it.
A resolver that cannot derive a sound bound for a dynamic dimension must omit it,
which keeps the D-037 `max_budget_draw` fallback. Probabilistic or optimistic
estimates are never valid.

## Delivered

- `gates.py`:
  - new types: `ReservationResolver` protocol (`resolve_reservation(request, spec)`),
    `ReservationOrigin` (`DECLARED | REQUEST_BOUND | MAX_FALLBACK`),
    `ResolvedReservation`;
  - new methods and parameters: `GatePipeline.resolve`, `GatePipeline(..., resolver=)`,
    `static_check/authorize(..., expected_reservation=)`;
  - `GatePipeline.check_side_effect` (G3, now public);
  - `FrozenRequest.reservation_origins`.
- Actual stage order: `static_check` runs G0, G1, G2a sizing, and G2b resolution
  plus validation. `check_budget` runs the G2 quota check, and only then
  `check_side_effect` runs G3. WorkBatch preflight sizes all items, runs the
  cumulative G2 check, then G3 for each item, all before any dispatch.
- `coordinator.py`:
  - `Coordinator(reservation_resolver=)`;
  - the preflight sizing (single request and every WorkBatch item) must be
    reproduced exactly at authorization;
  - the `EXECUTE` trace records `reserved` plus `reservation_origins`.
- `GateDecision`, `RequestGate`, `Executor`, `reconcile`, and B3 verification are
  unchanged.
- Tests: `tests/test_reservation.py` (26 tests) plus the unchanged B1–B3 suites.

## Requirement mapping

| # | Requirement | Test |
|---|---|---|
| 1, 15 | request-bound 600 under max 3600; origin traced | `test_request_bound_draw_is_reserved_instead_of_the_maximum` |
| 2, 3 | 600 fits 600; 599 denies at G2 before adapter | `test_request_bound_reservation_fits_exactly_and_one_unit_less_denies_before_adapter` |
| 4 | above max | `test_resolved_draw_above_the_tool_maximum_is_denied` |
| 5 | negative / non-numeric | `test_negative_or_non_numeric_resolved_draws_are_denied` |
| 6 | NaN / ±Inf | `test_non_finite_resolved_draws_are_denied` |
| 7 | undeclared / non-dynamic dimension | `test_resolver_cannot_introduce_or_override_dimensions` |
| 8 | missing resolver → max | `test_missing_resolver_or_omitted_dimension_reserves_the_maximum` |
| 9 | missing max stays unreservable | `test_dynamic_dimension_without_maximum_stays_unreservable` |
| 10 | numeric draw unchanged | `test_numeric_declared_draw_is_unchanged_and_never_resolved` |
| 11 | WorkBatch sums request-bound reservations | `test_work_batch_preflight_sums_request_bound_reservations_before_any_dispatch` |
| 12 | resolver exception / drift fails closed | `test_resolver_exception_fails_closed_without_internals`, `test_work_batch_with_failing_resolver_dispatches_nothing`, `test_work_batch_item_whose_reservation_drifts_fails_closed_alone` |
| 13 | SIMULATE rules unchanged | `test_simulation_classification_and_isolation_are_unchanged` |
| 14 | MUTATE approval/revision unchanged | `test_mutate_approval_and_revision_binding_are_unchanged` |
| 15 | FrozenRequest keeps G2 reservation | `test_frozen_request_keeps_exactly_the_g2_reservation`, `test_reservation_that_changes_between_preflight_and_authorization_is_denied`, `test_consumer_reservation_must_match_the_admitted_reservation` |
| 16 | reconciliation unchanged (actual < reservation charged) | `test_post_execution_reconciliation_is_unchanged` |
| 17 | determinism | `test_same_request_spec_and_context_give_identical_reservation_and_trace` |
| 18 | no evaluation | `test_no_expression_string_is_evaluated` (traps `eval`/`exec`/`compile`, AST scan of `src`) |
| — | G2 quota precedes G3 (evaluated, not just traced) | `test_g2_quota_denial_precedes_a_g3_simulation_denial`, `test_evaluation_order_matches_trace_order`, `test_work_batch_cumulative_g2_denial_precedes_item_g3_denial` |

## Notes for review

- G3's existing "SIMULATE must reserve a positive simulation draw" now applies to
  the resolved reservation. A resolver that sizes every simulation dimension to
  zero is denied `SIMULATION_DRAW_UNDECLARED`.
- `RESOLVER_ERROR` records only the exception type, not its message.
- The resolver runs twice per request (preflight sizing, then authorization). A
  non-reproducible answer is denied `RESERVATION_NOT_DETERMINISTIC` at G2. In a
  WorkBatch this fails only the drifting item; dependents are `SKIPPED_DEPENDENCY`.
- Origins are recorded on `FrozenRequest` and the `EXECUTE` event, not on the G2
  `GateDecision` (unchanged contract). A request denied before freeze has no origin
  map in the trace.
- Accepted v0 residual: `Executor.execute(request, spec)` is unchanged and does not
  receive the reservation. Trusted adapters are expected to obey the bounded request
  semantics. A successful overdraw is a reconciliation violation: it is charged
  `max(actual, reserved)` and never ingested. An adapter exception is charged the
  resolved reservation.

No `SPEC_CONFLICT`.
