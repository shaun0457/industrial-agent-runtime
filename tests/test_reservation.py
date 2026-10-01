"""B2.1 trusted request-bound budget reservation (D-048) — fake provider only."""

import ast
import contextlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import industrial_agent_runtime
from industrial_agent_runtime import (
    Action, Budget, Coordinator, FakeProvider, GatePipeline, ReservationOrigin,
    SideEffectClass, Task, TaskStatus, ToolSpec, TraceRecorder, WorkBatch, WorkItem,
)
from test_contracts import ConsumerStore, reference
from test_coordinator import Execute, Ingest, Verify, finish, scripted
from test_gates import SIM_POLICY, Approval, Consumer, Guard, call

S = SideEffectClass
RUNTIME_MODULES = tuple(
    module for name, module in sorted(vars(industrial_agent_runtime).items())
    if getattr(module, "__name__", "").startswith("industrial_agent_runtime.")
    and hasattr(module, "__file__"))
POLICY = replace(SIM_POLICY, tool_allowlist=frozenset({"span", "fixed", "cost"}))
SPAN_SCHEMA = {"type": "object", "required": ["units"],
               "properties": {"units": {"type": "integer", "minimum": 0}}}


def span(name, units, tool="span"):
    return call(name, tool, units=units)


def ask(name, units, tool="span"):
    return scripted(Action.TOOL_REQUEST, tool_request=span(name, units, tool))


class Resolver:
    """Trusted fixture resolver: the exact horizon is the requested unit count."""

    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn or (lambda request, spec: {"horizon": request.arguments["units"]})

    def resolve_reservation(self, request, spec):
        self.calls.append(request.request_id)
        return self.fn(request, spec)


class RequestBoundReservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConsumerStore()
        self.gate, self.executor, self.verifier = Consumer(self.store), Execute(), Verify()
        self.guard, self.approval, self.resolver = Guard(), Approval(), Resolver()
        self.trace = TraceRecorder(self.directory.name)
        self.executor.extra = {"rollouts": 1, "horizon": 600}
        self.set_budget(horizon=10_000)
        self.specs = {
            "span": ToolSpec("span", "", SPAN_SCHEMA, {"type": "object"}, S.SIMULATE,
                             ("sandbox",), {"rollouts": 1, "horizon": "arguments.units"},
                             {"rollouts": 1, "horizon": 3600}, "ISOLATED_BRANCH"),
            "fixed": ToolSpec("fixed", "", SPAN_SCHEMA, {"type": "object"}, S.SIMULATE,
                              ("sandbox",), {"rollouts": 2, "horizon": 30},
                              {"rollouts": 2, "horizon": 30}, "ISOLATED_BRANCH"),
            "cost": ToolSpec("cost", "", SPAN_SCHEMA, {"type": "object"}, S.MUTATE,
                             declared_budget_draw={"cost": "arguments.units"},
                             max_budget_draw={"cost": 100}),
        }

    def set_budget(self, horizon, cost=1000):
        self.budget = Budget(10, 10, 0, 0, 20, extra_dimensions={
            "rollouts": 10, "horizon": horizon, "cost": cost})
        self.task = Task("t1", "fixture objective", (reference(),),
                         ("span", "fixed", "cost"), self.budget,
                         {"type": "object", "required": ["answer"]})

    def run_script(self, turns, **overrides):
        options = dict(model_metadata={"provider": "fake", "model": "scripted",
                                       "model_version": "1",
                                       "prompt_template_version": "fixture-v1"},
                       gate=self.gate, executor=self.executor, verifier=self.verifier,
                       ingestor=Ingest(), gate_policy=POLICY, approval=self.approval,
                       reference_guard=self.guard, reservation_resolver=self.resolver,
                       clock=lambda: "2026-10-01T00:00:00Z")
        options.update(overrides)
        self.coordinator = Coordinator(self.task, self.store, FakeProvider(turns), self.trace,
                                       tuple(self.specs.values()), **options)
        return self.coordinator.run()

    def pipeline(self, resolver="default"):
        return GatePipeline(self.task, self.specs, POLICY, None, None, Guard(),
                            resolver=self.resolver if resolver == "default" else resolver)

    def events(self, kind):
        return [e for e in self.trace.read_events() if e["type"] == kind]

    def g2_reserved(self):
        return [e["output_summary"]["reserved_budget_draw"] for e in self.events("GATE")
                if e["output_summary"]["stage"] == "G2_BUDGET"
                and e["output_summary"]["decision"] == "ALLOW"]

    def denial(self):
        denied = [e["output_summary"] for e in self.events("GATE")
                  if e["output_summary"]["decision"] != "ALLOW"]
        return (denied[-1]["stage"], denied[-1]["reason_code"]) if denied else None

    def dispatched(self):
        return [c.request_id for c in self.executor.calls]

    def assert_resolver_denies(self, output, code):
        self.resolver.fn = lambda request, spec: output
        result = self.run_script([ask("a", 600), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", code))
        self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))
        self.assertEqual(result.budget_usage["tool_calls"], 0)

    # 1 / 15 ---------------------------------------------------------------------------
    def test_exact_request_bound_draw_is_reserved_instead_of_the_maximum(self):
        result = self.run_script([ask("a", 600), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.g2_reserved(), [{"rollouts": 1, "horizon": 600}])
        execute = self.events("EXECUTE")[0]["input_summary"]
        self.assertEqual(execute["reserved"], {"rollouts": 1, "horizon": 600})
        self.assertEqual(execute["reservation_origins"],
                         {"rollouts": ReservationOrigin.DECLARED,
                          "horizon": ReservationOrigin.REQUEST_BOUND})

    # 2 / 3 ----------------------------------------------------------------------------
    def test_exact_reservation_fits_exactly_and_one_unit_less_denies_before_adapter(self):
        self.set_budget(horizon=600)
        self.assertEqual(self.run_script([ask("a", 600), finish()]).status, TaskStatus.DONE)
        self.assertEqual(self.dispatched(), ["a"])
        self.setUp()
        self.set_budget(horizon=599)
        result = self.run_script([ask("a", 600), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "BUDGET_EXHAUSTED"))
        self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))
        self.assertEqual((result.budget_usage["tool_calls"], result.budget_usage["horizon"]),
                         (0, 0))
        # Without a resolver the same 600-unit request must reserve the 3600 maximum.
        self.setUp()
        self.set_budget(horizon=600)
        self.run_script([ask("a", 600), finish()], reservation_resolver=None)
        self.assertEqual(self.denial(), ("G2_BUDGET", "BUDGET_EXHAUSTED"))
        self.assertEqual(self.dispatched(), [])

    # 4 / 5 / 6 / 7 --------------------------------------------------------------------
    def test_resolved_draw_above_the_tool_maximum_is_denied(self):
        self.assert_resolver_denies({"horizon": 3601}, "RESOLVED_DRAW_EXCEEDS_MAX")

    def test_negative_or_non_numeric_resolved_draws_are_denied(self):
        for value in (-1, -0.5, True, False, "600", None, [600]):
            with self.subTest(value=value):
                self.setUp()
                self.assert_resolver_denies({"horizon": value}, "INVALID_RESOLVED_DRAW")

    def test_non_finite_resolved_draws_are_denied(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.setUp()
                self.assert_resolver_denies({"horizon": value}, "INVALID_RESOLVED_DRAW")

    def test_resolver_cannot_introduce_or_override_dimensions(self):
        cases = [({"horizon": 600, "optimizer_trials": 1}, "RESOLVED_DIMENSION_UNDECLARED"),
                 ({"tool_calls": 0}, "RESOLVED_DIMENSION_UNDECLARED"),
                 ({"cost": 1}, "RESOLVED_DIMENSION_UNDECLARED"),
                 ({"rollouts": 0}, "RESOLVED_DIMENSION_NOT_DYNAMIC"),
                 ([("horizon", 600)], "INVALID_RESOLVER_OUTPUT"),
                 ({1: 600}, "INVALID_RESOLVER_OUTPUT"),
                 (None, "INVALID_RESOLVER_OUTPUT")]
        for output, code in cases:
            with self.subTest(output=output):
                self.setUp()
                self.assert_resolver_denies(output, code)
        # A maximum-only dimension is not a dynamic declaration either.
        self.setUp()
        self.specs["span"] = replace(self.specs["span"], max_budget_draw={
            "rollouts": 1, "horizon": 3600, "cost": 5})
        self.assert_resolver_denies({"cost": 1}, "RESOLVED_DIMENSION_NOT_DYNAMIC")

    def test_none_declared_draw_is_traced_as_max_fallback(self):
        spec = replace(self.specs["fixed"], declared_budget_draw={"rollouts": None,
                                                                  "horizon": 30})
        resolved = self.pipeline().resolve(span("a", 1, "fixed"), spec)
        self.assertEqual(dict(resolved.draw), {"rollouts": 2, "horizon": 30})
        self.assertEqual(dict(resolved.origins),
                         {"rollouts": "MAX_FALLBACK", "horizon": "DECLARED"})

    def test_drift_is_reported_as_nondeterminism_before_g3(self):
        self.specs["span"] = replace(self.specs["span"], declared_budget_draw={
            "rollouts": "arguments.units", "horizon": "arguments.units"})
        answers = iter([{"rollouts": 1, "horizon": 600}, {"rollouts": 0, "horizon": 0}])
        self.resolver.fn = lambda request, spec: next(answers)
        self.run_script([ask("a", 600), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "RESERVATION_NOT_DETERMINISTIC"))
        self.assertEqual(self.dispatched(), [])

    # 8 / 9 / 10 -----------------------------------------------------------------------
    def test_missing_resolver_or_omitted_dimension_reserves_the_maximum(self):
        for resolver in (None, Resolver(lambda request, spec: {})):
            with self.subTest(resolver=resolver):
                resolved = self.pipeline(resolver).resolve(span("a", 600), self.specs["span"])
                self.assertEqual(dict(resolved.draw), {"rollouts": 1, "horizon": 3600})
                self.assertEqual(dict(resolved.origins),
                                 {"rollouts": "DECLARED", "horizon": "MAX_FALLBACK"})
        self.run_script([ask("a", 600), finish()], reservation_resolver=None)
        self.assertEqual(self.g2_reserved(), [{"rollouts": 1, "horizon": 3600}])
        self.assertEqual(self.events("EXECUTE")[0]["input_summary"]["reservation_origins"],
                         {"rollouts": "DECLARED", "horizon": "MAX_FALLBACK"})

    def test_dynamic_dimension_without_maximum_stays_unreservable(self):
        self.specs["span"] = replace(self.specs["span"], max_budget_draw={"rollouts": 1})
        self.run_script([ask("a", 600), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "UNRESERVABLE_DRAW"))
        self.assertEqual((self.dispatched(), self.resolver.calls), ([], []))

    def test_numeric_declared_draw_is_unchanged_and_never_resolved(self):
        self.executor.extra = {"rollouts": 2, "horizon": 30}
        result = self.run_script([ask("a", 600, "fixed"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.resolver.calls, [])
        self.assertEqual(self.g2_reserved(), [{"rollouts": 2, "horizon": 30}])
        self.assertEqual(self.events("EXECUTE")[0]["input_summary"]["reservation_origins"],
                         {"rollouts": "DECLARED", "horizon": "DECLARED"})
        self.assertEqual(self.pipeline().reservation("a", self.specs["fixed"]),
                         {"rollouts": 2, "horizon": 30})

    # 11 -------------------------------------------------------------------------------
    def test_work_batch_preflight_sums_exact_reservations_before_any_dispatch(self):
        batch = WorkBatch("b", "", (WorkItem("w1", "TOOL", (), span("a", 600)),
                                    WorkItem("w2", "TOOL", (), span("b", 600))))
        self.set_budget(horizon=1199)  # each item fits alone; the sum does not
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))
        batches = self.events("WORK_BATCH")
        self.assertIn("BUDGET_EXHAUSTED", batches[0]["output_summary"]["reason"])
        self.setUp()
        self.set_budget(horizon=1200)
        result = self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual((result.status, self.dispatched()), (TaskStatus.DONE, ["a", "b"]))
        self.assertEqual(self.g2_reserved(), [{"rollouts": 1, "horizon": 600}] * 2)
        self.assertEqual(result.budget_usage["horizon"], 1200)
        self.assertEqual(self.resolver.calls, ["a", "b", "a", "b"])  # preflight + authorize

    def test_work_batch_with_failing_resolver_dispatches_nothing(self):
        def flaky(request, spec):
            if request.request_id == "b":
                raise RuntimeError("resolver backend outage")
            return {"horizon": 1}
        self.resolver.fn = flaky
        batch = WorkBatch("b", "", (WorkItem("w1", "TOOL", (), span("a", 1)),
                                    WorkItem("w2", "TOOL", (), span("b", 1))))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual(self.dispatched(), [])
        self.assertIn("RESOLVER_ERROR", self.events("WORK_BATCH")[0]["output_summary"]["reason"])

    def test_work_batch_item_whose_reservation_drifts_fails_closed_alone(self):
        # A resolver that breaks the purity contract after preflight: the drifting
        # item is denied (never dispatched with an unchecked reservation); ALL_SETTLED
        # skips its dependents. Every dispatched item used its preflight reservation.
        seen = {}
        def drifting(request, spec):
            seen[request.request_id] = seen.get(request.request_id, 0) + 1
            bump = 1 if request.request_id == "b" and seen["b"] > 1 else 0
            return {"horizon": request.arguments["units"] + bump}
        self.resolver.fn = drifting
        batch = WorkBatch("b", "", (WorkItem("w1", "TOOL", (), span("a", 600)),
                                    WorkItem("w2", "TOOL", (), span("b", 600)),
                                    WorkItem("w3", "TOOL", ("w2",), span("c", 600))))
        self.set_budget(horizon=1800)
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual(self.dispatched(), ["a"])
        self.assertEqual(self.denial(), ("G2_BUDGET", "RESERVATION_NOT_DETERMINISTIC"))
        outcomes = self.events("WORK_BATCH")[-1]["output_summary"]
        self.assertEqual(outcomes, {"w1": "COMPLETED", "w2": "FAILED",
                                    "w3": "SKIPPED_DEPENDENCY"})
        self.assertEqual(self.g2_reserved(), [{"rollouts": 1, "horizon": 600}])

    # 12 -------------------------------------------------------------------------------
    def test_resolver_exception_fails_closed_without_internals(self):
        def broken(request, spec):
            raise RuntimeError("secret-consumer-detail")
        self.resolver.fn = broken
        self.run_script([ask("a", 600), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "RESOLVER_ERROR"))
        self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))
        self.assertNotIn(b"secret-consumer-detail", self.trace.events_path.read_bytes())

    # 13 -------------------------------------------------------------------------------
    def test_simulation_classification_and_isolation_are_unchanged(self):
        cases = {
            "NO_ISOLATION_GUARANTEE": replace(self.specs["span"], isolation_guarantee="NONE"),
            "SIMULATION_MISCLASSIFIED": replace(self.specs["span"], side_effect_class=S.COMPUTE,
                                                isolation_guarantee=None),
            "SIMULATION_DRAW_UNDECLARED": replace(self.specs["span"], declared_budget_draw={
                "rollouts": "arguments.units", "horizon": "arguments.units"}),
        }
        for code, spec in cases.items():
            with self.subTest(code):
                self.setUp()
                self.resolver.fn = lambda request, spec: {"rollouts": 0, "horizon": 0}
                if code != "SIMULATION_DRAW_UNDECLARED":
                    self.resolver.fn = lambda request, spec: {"horizon": 1}
                self.specs["span"] = spec
                self.run_script([ask("a", 1), finish()])
                self.assertEqual(self.denial(), ("G3_SIDE_EFFECT", code))
                self.assertEqual(self.dispatched(), [])
        # Authority comes first: an ungranted class never reaches the resolver.
        self.setUp()
        self.run_script([ask("a", 1), finish()], gate_policy=replace(
            POLICY, granted_side_effect_classes=frozenset({S.READ})))
        self.assertEqual(self.denial(), ("G1_AUTHORITY", "CLASS_NOT_GRANTED"))
        self.assertEqual(self.resolver.calls, [])

    # 14 / 15 --------------------------------------------------------------------------
    def test_mutate_approval_and_revision_binding_are_unchanged(self):
        self.resolver.fn = lambda request, spec: {"cost": request.arguments["units"]}
        self.executor.extra = {"cost": 5}
        self.guard.revision = "ref-7"
        self.approval.verdict = "DENY"
        self.run_script([ask("a", 5, "cost"), finish()])
        self.assertEqual(self.denial(), ("APPROVAL", "NOT_APPROVED"))
        self.assertEqual(self.dispatched(), [])
        frozen = self.approval.seen[0]
        self.assertEqual((dict(frozen.reserved_budget_draw), dict(frozen.reservation_origins)),
                         ({"cost": 5}, {"cost": "REQUEST_BOUND"}))
        self.assertEqual((frozen.expected_state_revision, frozen.expected_reference_revision),
                         (0, "ref-7"))
        self.setUp()
        self.resolver.fn = lambda request, spec: {"cost": request.arguments["units"]}
        self.approval.side_effect = lambda frozen: setattr(self.guard, "revision", 1)
        self.run_script([ask("a", 5, "cost"), finish()])
        self.assertEqual(self.denial(), ("DISPATCH", "STALE_REFERENCE_REVISION"))
        self.assertEqual(self.dispatched(), [])

    def test_frozen_request_keeps_exactly_the_g2_reservation(self):
        self.approval.side_effect = lambda frozen: self.assertRaises(
            FrozenInstanceError, setattr, frozen, "reserved_budget_draw", {"horizon": 1})
        policy = replace(POLICY, approval_required_for=frozenset({S.MUTATE, S.SIMULATE}))
        result = self.run_script([ask("a", 600), finish()], gate_policy=policy)
        self.assertEqual(result.status, TaskStatus.DONE)
        frozen = self.approval.seen[0]
        g2 = self.g2_reserved()[0]
        execute = self.events("EXECUTE")[0]["input_summary"]
        self.assertEqual(dict(frozen.reserved_budget_draw), g2)
        self.assertEqual(execute["reserved"], g2)
        self.assertEqual(execute["reservation_origins"], dict(frozen.reservation_origins))
        self.assertEqual(self.events("RECONCILIATION")[0]["output_summary"]["reserved"], g2)

    def test_reservation_that_changes_between_preflight_and_authorization_is_denied(self):
        answers = iter([600, 601])
        self.resolver.fn = lambda request, spec: {"horizon": next(answers)}
        self.run_script([ask("a", 600), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "RESERVATION_NOT_DETERMINISTIC"))
        self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))

    def test_consumer_reservation_must_match_the_exact_reservation(self):
        for claimed, outcome in (({"rollouts": 1, "horizon": 600}, ["a"]),
                                 ({"rollouts": 1, "horizon": 3600}, [])):
            with self.subTest(claimed=claimed):
                self.setUp()
                self.gate.validate_request = (
                    lambda *a, c=claimed: replace(Consumer.validate_request(self.gate, *a),
                                                  reserved_budget_draw=c))
                self.run_script([ask("a", 600), finish()])
                self.assertEqual(self.dispatched(), outcome)
                if not outcome:
                    self.assertEqual(self.denial(), ("CONSUMER", "RESERVATION_MISMATCH"))

    # 16 -------------------------------------------------------------------------------
    def test_post_execution_reconciliation_is_unchanged(self):
        self.executor.extra = {"rollouts": 1, "horizon": 450}
        result = self.run_script([ask("a", 600), finish()])
        self.assertEqual(result.budget_usage["horizon"], 450)  # actual, not reservation
        self.assertEqual(self.events("RECONCILIATION")[0]["status"], "RECONCILED")
        for actual in ({"rollouts": 1, "horizon": 601}, {"rollouts": 1},
                       {"rollouts": 1, "horizon": 1, "optimizer_trials": 1}):
            with self.subTest(actual=actual):
                self.setUp()
                self.executor.extra = actual
                result = self.run_script([ask("a", 600), finish()])
                self.assertEqual(self.events("RECONCILIATION")[0]["status"], "VIOLATION")
                self.assertEqual(self.store.data, {})  # never ingested
                self.assertGreaterEqual(result.budget_usage["horizon"], 600)
        self.setUp()
        def explode(request, spec):
            raise RuntimeError("adapter crashed")
        self.executor.execute = explode
        result = self.run_script([ask("a", 600), finish()])
        self.assertEqual(result.budget_usage["horizon"], 600)  # full exact reservation
        self.assertEqual(self.store.data, {})

    # 17 / 18 --------------------------------------------------------------------------
    def test_same_request_spec_and_context_give_identical_reservation_and_trace(self):
        pipeline = self.pipeline()
        first = pipeline.resolve(span("a", 600), self.specs["span"])
        self.assertEqual(first, pipeline.resolve(span("a", 600), self.specs["span"]))
        script = [ask("a", 600), ask("b", 700), ask("c", 4000), finish()]
        self.run_script(script)
        trace = self.trace.events_path.read_bytes()
        self.setUp()
        self.run_script(script)
        self.assertEqual(trace, self.trace.events_path.read_bytes())

    def test_no_expression_string_is_evaluated(self):
        expression = "__import__('os').getpid() * 0 + 1"
        calls = []
        def trap(*args, **kwargs):
            calls.append(args)
            raise AssertionError("dynamic evaluation attempted")
        # Shadow the builtins only inside runtime modules, so unrelated imports or
        # dataclass creation elsewhere cannot trip the trap.
        with contextlib.ExitStack() as stack:
            for module in RUNTIME_MODULES:
                for name in ("eval", "exec", "compile", "__import__"):
                    stack.enter_context(mock.patch.object(module, name, trap, create=True))
            for resolver in (self.resolver, None):
                self.setUp()
                self.specs["span"] = replace(self.specs["span"], declared_budget_draw={
                    "rollouts": 1, "horizon": expression})
                result = self.run_script([ask("a", 600), finish()],
                                         reservation_resolver=resolver)
                self.assertEqual(result.status, TaskStatus.DONE)
                self.assertEqual(self.g2_reserved()[0]["horizon"],
                                 600 if resolver else 3600)
        self.assertEqual(calls, [])
        forbidden = {"eval", "exec", "compile", "__import__"}
        source = Path(__file__).resolve().parents[1] / "src" / "industrial_agent_runtime"
        for path in sorted(source.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, forbidden, f"{path.name}:{node.lineno}")


if __name__ == "__main__":
    unittest.main()
