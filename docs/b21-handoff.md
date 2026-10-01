# B2.1 request-bound reservation handoff

Branch `feat/request-bound-reservation-v0`, base `main` `2f243bd`.
Owning spec: `docs/specs/deterministic-gates-v0.md`, section "B2.1 — Request-bound
budget reservation (D-048)". Resolves C4 SC-5; D-037 is unchanged.

## Delivered

- `gates.py`: `ReservationResolver` protocol (`resolve_reservation(request, spec)`),
  `ReservationOrigin` (`DECLARED | REQUEST_BOUND | MAX_FALLBACK`),
  `ResolvedReservation`, `GatePipeline.resolve`, `GatePipeline(..., resolver=)`,
  `authorize(..., expected_reservation=)`, `FrozenRequest.reservation_origins`.
- `coordinator.py`: `Coordinator(reservation_resolver=)`; preflight sizing (single
  request and every WorkBatch item) must be reproduced exactly at authorization;
  the `EXECUTE` trace records `reserved` plus `reservation_origins`.
- `GateDecision`, `RequestGate`, `reconcile`, and B3 verification are unchanged.
- Tests: `tests/test_reservation.py` (23 tests) plus the unchanged B1–B3 suites.

## Requirement mapping

| # | Requirement | Test |
|---|---|---|
| 1, 15 | exact 600 under max 3600; origin traced | `test_exact_request_bound_draw_is_reserved_instead_of_the_maximum` |
| 2, 3 | 600 fits 600; 599 denies at G2 before adapter | `test_exact_reservation_fits_exactly_and_one_unit_less_denies_before_adapter` |
| 4 | above max | `test_resolved_draw_above_the_tool_maximum_is_denied` |
| 5 | negative / non-numeric | `test_negative_or_non_numeric_resolved_draws_are_denied` |
| 6 | NaN / ±Inf | `test_non_finite_resolved_draws_are_denied` |
| 7 | undeclared / non-dynamic dimension | `test_resolver_cannot_introduce_or_override_dimensions` |
| 8 | missing resolver → max | `test_missing_resolver_or_omitted_dimension_reserves_the_maximum` |
| 9 | missing max stays unreservable | `test_dynamic_dimension_without_maximum_stays_unreservable` |
| 10 | numeric draw unchanged | `test_numeric_declared_draw_is_unchanged_and_never_resolved` |
| 11 | WorkBatch sums exact reservations | `test_work_batch_preflight_sums_exact_reservations_before_any_dispatch` |
| 12 | resolver exception / drift fails closed | `test_resolver_exception_fails_closed_without_internals`, `test_work_batch_with_failing_resolver_dispatches_nothing` |
| 13 | SIMULATE rules unchanged | `test_simulation_classification_and_isolation_are_unchanged` |
| 14 | MUTATE approval/revision unchanged | `test_mutate_approval_and_revision_binding_are_unchanged` |
| 15 | FrozenRequest keeps G2 reservation | `test_frozen_request_keeps_exactly_the_g2_reservation`, `test_reservation_that_changes_between_preflight_and_authorization_is_denied`, `test_consumer_reservation_must_match_the_exact_reservation` |
| 16 | reconciliation unchanged | `test_post_execution_reconciliation_is_unchanged` |
| 17 | determinism | `test_same_request_spec_and_context_give_identical_reservation_and_trace` |
| 18 | no evaluation | `test_no_expression_string_is_evaluated` (traps `eval`/`exec`/`compile`, AST scan of `src`) |

## Notes for review

- G3's existing "SIMULATE must reserve a positive simulation draw" now applies to
  the resolved reservation, so a resolver that sizes every simulation dimension to
  zero is denied `SIMULATION_DRAW_UNDECLARED`.
- `RESOLVER_ERROR` records only the exception type, not its message.
- The resolver runs twice per request (preflight sizing, then authorization); a
  non-reproducible answer is denied `RESERVATION_NOT_DETERMINISTIC` at G2, before G3. In a WorkBatch this fails only the drifting item
  (`test_work_batch_item_whose_reservation_drifts_fails_closed_alone`).
- Origins are recorded on `FrozenRequest` and the `EXECUTE` event, not on the G2
  `GateDecision` (unchanged contract); a request denied before freeze has no origin
  map in the trace.

- Residual risk: `Executor.execute(request, spec)` is unchanged and does not receive
  the reservation, so adapters must bound actual use to the request. Overdraw beyond
  an exact reservation is a reconciliation violation (charged, not ingested); an
  adapter exception is charged the exact reservation. Tightening this would change
  the frozen Executor contract and is out of B2.1 scope.

No `SPEC_CONFLICT`.
