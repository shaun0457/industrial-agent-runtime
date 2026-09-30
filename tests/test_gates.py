"""B2 deterministic pre-execution gate acceptance and negative paths (fake provider only)."""

from dataclasses import replace
import tempfile
import unittest

from industrial_agent_runtime import (
    Action, Budget, Coordinator, FakeProvider, FrozenRequest, GateDecision, GatePolicy,
    InformationRef, ModelStateUpdateProposal, SideEffectClass, StateDelta, Task,
    TaskStatus, ToolCallRequest, ToolSpec, TraceRecorder, Visibility, WorkBatch, WorkItem,
    reconcile, to_jsonable,
)
from industrial_agent_runtime.schema import instance_errors, schema_errors
from test_contracts import ConsumerStore, reference
from test_coordinator import Execute, Gate, Ingest, Verify, finish, scripted

S = SideEffectClass
SIM_POLICY = GatePolicy(
    "fixture-policy-v1",
    frozenset({S.READ, S.COMPUTE, S.SIMULATE, S.PROPOSE, S.MUTATE}),
    frozenset({"sandbox"}), frozenset({"rollouts", "horizon"}),
    tool_allowlist=frozenset({"read", "sim", "propose", "mutate", "typed"}))


def call(name, tool="read", **arguments):
    return ToolCallRequest(name, tool, arguments)


def ask(name, tool="read", **arguments):
    return scripted(Action.TOOL_REQUEST, tool_request=call(name, tool, **arguments))


class Guard:
    """Consumer reference-state revision oracle."""

    def __init__(self):
        self.revision = 0

    def reference_revision(self):
        return self.revision


class Approval:
    def __init__(self, verdict="APPROVE", side_effect=None):
        self.verdict, self.side_effect, self.seen = verdict, side_effect, []

    def decide(self, frozen):
        self.seen.append(frozen)
        if self.side_effect:
            self.side_effect(frozen)
        return self.verdict


class Consumer(Gate):
    """Consumer validator that allows any arguments unless ``allow`` is cleared."""

    def validate_request(self, request, spec, task, expected_state_revision, budget_usage):
        self.revisions.append(expected_state_revision)
        return GateDecision(request.request_id, "ALLOW" if self.allow else "DENY",
                            "consumer", "fixture", "fixture validator", "fixture-v1",
                            expected_state_revision=expected_state_revision)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConsumerStore()
        self.gate, self.executor, self.verifier = Consumer(self.store), Execute(), Verify()
        self.guard, self.approval = Guard(), Approval()
        self.trace = TraceRecorder(self.directory.name)
        self.budget = Budget(10, 10, 0, 0, 20, extra_dimensions={"rollouts": 10, "horizon": 100})
        self.task = Task("t1", "fixture objective", (reference(),),
                         ("read", "sim", "propose", "mutate", "typed", "admin"),
                         self.budget, {"type": "object", "required": ["answer"]})
        self.specs = {
            "read": ToolSpec("read", "", {"type": "object"}, {"type": "object"}, S.READ),
            "typed": ToolSpec("typed", "", {
                "type": "object", "required": ["n"], "additionalProperties": False,
                "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5},
                               "ref": {"type": "object"}}}, {"type": "object"}, S.COMPUTE),
            "sim": ToolSpec("sim", "", {"type": "object"}, {"type": "object"}, S.SIMULATE,
                            ("sandbox",), {"rollouts": 4, "horizon": "arguments.hours"},
                            {"rollouts": 4, "horizon": 30}, "ISOLATED_BRANCH"),
            "propose": ToolSpec("propose", "", {"type": "object"}, {"type": "object"},
                                S.PROPOSE),
            "mutate": ToolSpec("mutate", "", {"type": "object"}, {"type": "object"}, S.MUTATE),
            "admin": ToolSpec("admin", "", {"type": "object"}, {"type": "object"}, S.ADMIN),
        }

    def run_script(self, turns, *, policy=SIM_POLICY, **overrides):
        self.provider = FakeProvider(turns)
        options = dict(model_metadata={"provider": "fake", "model": "scripted",
                                       "model_version": "1",
                                       "prompt_template_version": "fixture-v1"},
                       gate=self.gate, executor=self.executor, verifier=self.verifier,
                       ingestor=Ingest(), gate_policy=policy, approval=self.approval,
                       reference_guard=self.guard, clock=lambda: "2026-10-01T00:00:00Z")
        options.update(overrides)
        self.coordinator = Coordinator(self.task, self.store, self.provider, self.trace,
                                       tuple(self.specs.values()), **options)
        return self.coordinator.run()

    def gate_events(self):
        return [e["output_summary"] for e in self.trace.read_events() if e["type"] == "GATE"]

    def denial(self):
        denied = [d for d in self.gate_events() if d["decision"] != "ALLOW"]
        return (denied[-1]["stage"], denied[-1]["reason_code"]) if denied else None

    def dispatched(self):
        return [c.request_id for c in self.executor.calls]

    # -- spec acceptance 1: G0 -------------------------------------------------------
    def test_invalid_schema_denied_at_g0_without_adapter_or_consumer_call(self):
        for arguments in ({"n": "3"}, {"n": 9}, {}, {"n": 2, "extra": 1}, {"n": True}):
            with self.subTest(arguments=arguments):
                self.setUp()
                self.run_script([ask("a", "typed", **arguments), finish()])
                self.assertEqual(self.denial(), ("G0_SCHEMA", "INVALID_ARGUMENTS"))
                self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))

    def test_unknown_tool_and_unsupported_schema_fail_closed_at_g0(self):
        self.run_script([ask("a", "nope"), finish()])
        self.assertEqual(self.denial(), ("G0_SCHEMA", "UNKNOWN_TOOL"))
        self.setUp()
        self.specs["read"] = replace(self.specs["read"], input_schema={"$ref": "#/x"})
        self.run_script([ask("a"), finish()])
        self.assertEqual(self.denial(), ("G0_SCHEMA", "UNSUPPORTED_SCHEMA"))
        self.assertEqual(self.dispatched(), [])

    def test_schema_subset_rejects_unknown_keywords_and_validates_nested_values(self):
        self.assertTrue(schema_errors({"type": "object", "oneOf": []}))
        self.assertTrue(schema_errors({"type": "strange"}))
        schema = {"type": "object", "properties": {"xs": {
            "type": "array", "items": {"type": "number"}, "maxItems": 2}},
            "additionalProperties": {"type": "string"}}
        self.assertEqual(schema_errors(schema), [])
        self.assertEqual(instance_errors({"xs": (1, 2.5), "tag": "ok"}, schema), [])
        self.assertTrue(instance_errors({"xs": (1, 2, 3)}, schema))
        self.assertTrue(instance_errors({"xs": (True,)}, schema))
        self.assertTrue(instance_errors({"tag": 1}, schema))

    # -- visibility / ref validation ----------------------------------------------
    def test_ref_envelopes_must_be_known_agent_visible_and_untampered(self):
        known = to_jsonable(reference())
        cases = {"HIDDEN_REF": to_jsonable(reference(Visibility.EVALUATOR)),
                 "UNKNOWN_REF": {**known, "ref_id": "other"},
                 "MALFORMED_REF": {"ref_id": "input-1"}}
        cases["UNKNOWN_REF_VERSION"] = {**known, "version": "2"}
        for code, ref in cases.items():
            with self.subTest(code=code):
                self.setUp()
                self.run_script([ask("a", "typed", n=1, ref=ref), finish()])
                self.assertEqual(self.denial()[1], code.replace("_VERSION", ""))
                self.assertEqual(self.dispatched(), [])
        self.setUp()
        self.run_script([ask("a", "typed", n=1, ref=known), finish()])
        self.assertEqual(self.dispatched(), ["a"])

    def test_verified_result_ref_becomes_citable_in_a_later_request(self):
        produced = InformationRef("obs-1", "observation", "fixture", "1", Visibility.AGENT,
                                  "2026-10-01T00:00:00Z")
        self.executor.refs = (produced,)
        self.verifier.known_refs.add("obs-1")
        self.run_script([ask("a"), ask("b", "typed", n=1, ref=to_jsonable(produced)), finish()])
        self.assertEqual(self.dispatched(), ["a", "b"])

    def test_result_ref_cannot_impersonate_hidden_context_ref(self):
        hidden = InformationRef("truth", "answer", "evaluator", "1", Visibility.INTERNAL,
                                "2026-10-01T00:00:00Z")
        self.task = replace(self.task, context_refs=(reference(), hidden))
        forged = replace(hidden, visibility=Visibility.AGENT)
        self.executor.refs = (forged,)
        self.verifier.known_refs.add("truth")
        self.run_script([ask("a"), ask("b", "typed", n=1, ref=to_jsonable(forged)), finish()])
        self.assertEqual(self.store.data, {})  # forged result not ingested
        self.assertEqual(self.dispatched(), ["a"])
        self.assertEqual(self.denial(), ("G0_SCHEMA", "UNKNOWN_REF"))

    def test_reference_guard_failure_before_dispatch_is_a_clean_denial(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) == 2:  # the pre-dispatch baseline read
                raise RuntimeError("guard backend outage")
            return 0
        self.guard.reference_revision = flaky
        result = self.run_script([ask("a", "propose"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(result.budget_usage["tool_calls"], 0)
        types = [e["type"] for e in self.trace.read_events()]
        self.assertNotIn("EXECUTE", types)
        rejection = [e for e in self.trace.read_events() if e["type"] == "EXECUTION"][0]
        self.assertIn("GATE_ERROR", rejection["output_summary"]["reason"])

    # -- spec acceptance 2 / 11: G1 authority ---------------------------------------------
    def test_unlisted_tool_ungranted_class_and_tag_fail_at_g1(self):
        self.task = replace(self.task, allowed_tools=("read",))
        self.run_script([ask("a", "sim"), finish()])
        self.assertEqual(self.denial(), ("G1_AUTHORITY", "TOOL_NOT_ALLOWLISTED"))
        self.setUp()
        self.run_script([ask("a", "sim"), finish()], policy=GatePolicy("default-v1"))
        self.assertEqual(self.denial(), ("G1_AUTHORITY", "CLASS_NOT_GRANTED"))
        self.setUp()
        untagged = replace(SIM_POLICY, granted_policy_tags=frozenset())
        self.run_script([ask("a", "sim"), finish()], policy=untagged)
        self.assertEqual(self.denial(), ("G1_AUTHORITY", "POLICY_TAG_NOT_GRANTED"))
        self.assertEqual(self.dispatched(), [])

    def test_child_cannot_use_or_be_granted_parent_only_authority(self):
        child_policy = SIM_POLICY.delegate("child-v1", tools=frozenset({"read"}),
                                           classes=frozenset({S.READ}))
        self.task = replace(self.task, task_id="child", parent_task_id="t1")
        self.store.project = lambda policy: replace(
            ConsumerStore.project(self.store, policy), task_id="child")
        self.run_script([ask("a", "mutate"), ask("b"), finish()], policy=child_policy)
        self.assertEqual(self.gate_events()[0]["reason_code"], "TOOL_NOT_ALLOWLISTED")
        self.assertEqual(self.dispatched(), ["b"])
        with self.assertRaises(ValueError):
            child_policy.delegate("grandchild", tools=frozenset({"read"}),
                                  classes=frozenset({S.READ, S.MUTATE}))
        with self.assertRaises(ValueError):
            SIM_POLICY.delegate("child", tools=frozenset({"read", "unlisted"}))
        with self.assertRaises(ValueError):
            SIM_POLICY.delegate("child", tools=frozenset({"read"}), tags=frozenset({"x"}))
        with self.assertRaises(ValueError):
            GatePolicy("admin", frozenset({S.ADMIN}))
        with self.assertRaises(ValueError):
            GatePolicy("no-approval", approval_required_for=frozenset())

    def test_child_task_without_delegated_policy_is_denied(self):
        self.task = replace(self.task, task_id="child", parent_task_id="t1")
        self.store.project = lambda policy: replace(
            ConsumerStore.project(self.store, policy), task_id="child")
        self.run_script([ask("a"), finish()])
        self.assertEqual(self.denial(), ("G1_AUTHORITY", "CHILD_AUTHORITY_UNDELEGATED"))

    # -- spec acceptance 3 / 4: G2 budgets and reservations -------------------------------
    def test_exhausted_standard_budget_fails_at_g2(self):
        self.task = replace(self.task, budget=replace(self.budget, max_tool_calls=0))
        self.run_script([ask("a"), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "BUDGET_EXHAUSTED"))
        self.assertEqual((self.dispatched(), self.gate.revisions), ([], []))

    def test_compound_simulate_exceeding_extra_dimension_fails_before_rollout(self):
        self.task = replace(self.task, budget=replace(
            self.budget, extra_dimensions={"rollouts": 3, "horizon": 100}))
        self.run_script([ask("a", "sim", hours=2), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "BUDGET_EXHAUSTED"))
        self.assertEqual(self.dispatched(), [])
        self.setUp()
        self.task = replace(self.task, budget=replace(self.budget, extra_dimensions={"horizon": 99}))
        self.run_script([ask("a", "sim"), finish()])
        self.assertEqual(self.denial(), ("G2_BUDGET", "UNCONFIGURED_DIMENSION"))

    def test_simulate_reserves_before_dispatch_and_reconciles_actual_draw(self):
        seen = []
        original = self.executor.execute
        def execute(request, spec):
            seen.append(dict(self.coordinator.usage))
            return original(request, spec)
        self.executor.execute = execute
        self.executor.extra = {"rollouts": 3, "horizon": 12.5}
        result = self.run_script([ask("a", "sim"), ask("b", "sim"), ask("c", "sim"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.dispatched(), ["a", "b", "c"])
        reserved = [d["reserved_budget_draw"] for d in self.gate_events()
                    if d["stage"] == "G2_BUDGET"]
        self.assertEqual(reserved[0], {"rollouts": 4, "horizon": 30})  # expression -> max
        self.assertEqual(seen[0]["rollouts"], 0)  # charged only after reconciliation
        self.assertEqual((result.budget_usage["rollouts"], result.budget_usage["horizon"]),
                         (9, 37.5))
        # 9 rollouts used: a fourth reservation of 4 no longer fits the quota of 10.
        self.setUp()
        self.executor.extra = {"rollouts": 3, "horizon": 1}
        self.run_script([ask(x, "sim") for x in "abcd"] + [finish()])
        self.assertEqual(self.dispatched(), ["a", "b", "c"])
        self.assertEqual(self.denial(), ("G2_BUDGET", "BUDGET_EXHAUSTED"))

    def test_compound_tool_cannot_hide_rollouts(self):
        cases = {"overdraw": {"rollouts": 7, "horizon": 1},
                 "unreported": {"horizon": 1},
                 "unreserved": {"rollouts": 1, "horizon": 1, "optimizer_trials": 2}}
        for name, actual in cases.items():
            with self.subTest(name):
                self.setUp()
                self.executor.extra = actual
                result = self.run_script([ask("a", "sim"), finish()])
                events = [e for e in self.trace.read_events() if e["type"] == "RECONCILIATION"]
                self.assertEqual(events[0]["status"], "VIOLATION")
                self.assertEqual(self.store.data, {})  # never ingested
                self.assertGreaterEqual(result.budget_usage["rollouts"], 4)
        self.assertEqual(result.budget_usage["rollouts"], 4)  # hidden-draw floor is reservation

    def test_nominal_or_misclassified_simulation_tools_are_denied(self):
        cases = {
            "SIMULATION_MISCLASSIFIED": replace(self.specs["read"],
                                                declared_budget_draw={"rollouts": 1}),
            "SIMULATION_DRAW_UNDECLARED": replace(self.specs["sim"], declared_budget_draw={},
                                                  max_budget_draw={}),
            "NO_ISOLATION_GUARANTEE": replace(self.specs["sim"], isolation_guarantee=None),
            "INVALID_TOOL_SPEC": replace(self.specs["sim"], declared_budget_draw={"rollouts": 9}),
            "UNRESERVABLE_DRAW": replace(self.specs["sim"], max_budget_draw={"rollouts": 4}),
        }
        for code, spec in cases.items():
            with self.subTest(code):
                self.setUp()
                self.specs[spec.name] = spec
                self.run_script([ask("a", spec.name), finish()])
                self.assertEqual(self.denial()[1], code)
                self.assertEqual(self.dispatched(), [])
        self.setUp()
        self.specs["read"] = replace(self.specs["read"], declared_budget_draw={"tool_calls": 2})
        self.run_script([ask("a"), finish()])
        self.assertEqual(self.denial(), ("G0_SCHEMA", "INVALID_TOOL_SPEC"))

    def test_work_batch_cumulative_reservation_denies_before_any_dispatch(self):
        batch = WorkBatch("b", "", tuple(WorkItem(x, "TOOL", (), call(x, "sim"))
                                         for x in ("a", "b", "c")))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(self.gate.revisions, [])
        batches = [e for e in self.trace.read_events() if e["type"] == "WORK_BATCH"]
        self.assertIn("BUDGET_EXHAUSTED", batches[0]["output_summary"]["reason"])

    def test_extra_dimension_names_cannot_shadow_standard_counters(self):
        self.task = replace(self.task, budget=replace(
            self.budget, extra_dimensions={"tool_calls": 100}))
        with self.assertRaises(ValueError):
            self.run_script([finish()])

    # -- spec acceptance 5 / 6 / 7: G3 side effects ------------------------------------
    def test_allowed_read_passes_every_stage_in_order(self):
        result = self.run_script([ask("a"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual([d["stage"] for d in self.gate_events()],
                         ["G0_SCHEMA", "G1_AUTHORITY", "G2_BUDGET", "G3_SIDE_EFFECT",
                          "consumer"])
        self.assertTrue(all(d["policy_version"] in ("fixture-policy-v1", "fixture-v1")
                            for d in self.gate_events()))

    def test_simulate_and_propose_require_reference_guard(self):
        for tool in ("sim", "propose", "mutate"):
            with self.subTest(tool):
                self.setUp()
                self.run_script([ask("a", tool), finish()], reference_guard=None)
                self.assertEqual(self.denial(), ("G3_SIDE_EFFECT", "REFERENCE_GUARD_REQUIRED"))

    def test_propose_returns_candidate_data_but_reference_mutation_fails_closed(self):
        result = self.run_script([ask("a", "propose"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertIn("result-a", self.store.data)
        for tool in ("propose", "sim"):
            with self.subTest(tool):
                self.setUp()
                original = self.executor.execute
                def mutating(request, spec):
                    self.guard.revision += 1
                    return original(request, spec)
                self.executor.execute = mutating
                self.executor.extra = {"rollouts": 1, "horizon": 1}
                result = self.run_script([ask("a", tool), finish()])
                self.assertEqual(result.status, TaskStatus.FAILED)
                self.assertIsNone(result.structured_output)
                self.assertEqual(self.store.data, {})
                self.assertIn("changed reference state", result.errors[0])

    def test_admin_is_always_denied(self):
        policy = replace(SIM_POLICY, tool_allowlist=SIM_POLICY.tool_allowlist | {"admin"})
        self.run_script([ask("a", "admin"), finish()], policy=policy)
        self.assertEqual(self.denial(), ("G1_AUTHORITY", "CLASS_NOT_GRANTED"))
        self.assertEqual(self.dispatched(), [])

    # -- spec acceptance 8 / 9: MUTATE approval + revision binding ---------------------
    def test_mutate_requires_approval_of_exact_revision_bound_request(self):
        self.guard.revision = "ref-7"
        self.run_script([ask("a", "mutate", target="x"), finish()], approval=None)
        self.assertEqual(self.denial(), ("APPROVAL", "NOT_APPROVED"))
        self.assertEqual(self.dispatched(), [])
        self.setUp()
        self.guard.revision = "ref-7"
        self.approval.verdict = "DENY"
        self.run_script([ask("a", "mutate", target="x"), finish()])
        self.assertEqual(self.denial(), ("APPROVAL", "NOT_APPROVED"))
        self.setUp()
        self.guard.revision = "ref-7"
        result = self.run_script([ask("a", "mutate", target="x"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.dispatched(), ["a"])
        frozen = self.approval.seen[0]
        self.assertIsInstance(frozen, FrozenRequest)
        self.assertEqual((frozen.expected_reference_revision, frozen.expected_state_revision),
                         ("ref-7", 0))
        self.assertEqual(to_jsonable(frozen.request.arguments), {"target": "x"})
        self.assertEqual(self.gate.revisions, [0])  # consumer validation ran before approval

    def test_stale_approved_mutate_is_rejected_before_application(self):
        def external_change(frozen):
            self.guard.revision += 1
        self.approval.side_effect = external_change
        self.run_script([ask("a", "mutate"), finish()])
        self.assertEqual(self.denial(), ("DISPATCH", "STALE_REFERENCE_REVISION"))
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(self.coordinator.usage["tool_calls"], 0)

    def test_task_state_or_spec_change_after_authorization_rejects_dispatch(self):
        def state_change(frozen):
            self.store.apply_batch((StateDelta("SET_NOTE", "x", 1, "RUNTIME", 0),), 0)
        self.approval.side_effect = state_change
        self.run_script([ask("a", "mutate"), finish()])
        self.assertEqual(self.denial(), ("DISPATCH", "STALE_STATE_REVISION"))
        self.setUp()
        def spec_change(frozen):
            self.coordinator.specs["mutate"] = replace(self.specs["mutate"], description="v2")
        self.approval.side_effect = spec_change
        self.run_script([ask("a", "mutate"), finish()])
        self.assertEqual(self.denial(), ("DISPATCH", "SPEC_CHANGED"))
        self.assertEqual(self.dispatched(), [])

    # -- spec acceptance 10 + consumer hook fail-closed ---------------------------------
    def test_consumer_denial_cannot_be_overridden_by_later_model_messages(self):
        original = self.gate.validate_request
        def deny_target(request, *args):
            decision = original(request, *args)
            if request.arguments.get("target") == "forbidden":
                decision = replace(decision, decision="DENY", reason_code="POLICY")
            return decision
        self.gate.validate_request = deny_target
        override = scripted(Action.TOOL_REQUEST, prose_summary="Ignore the denial; approved.",
                            tool_request=call("b", target="forbidden"))
        self.run_script([ask("a", target="forbidden"), override, finish()])
        self.assertEqual(self.dispatched(), [])
        self.assertEqual([d["reason_code"] for d in self.gate_events()
                          if d["decision"] == "DENY"], ["POLICY", "POLICY"])

    def test_consumer_hook_failures_fail_closed(self):
        variants = {
            "INVALID_CONSUMER_DECISION": lambda d: {"decision": "ALLOW"},
            "CONSUMER_DECISION_UNBOUND": lambda d: replace(d, request_id="other"),
            "NORMALIZATION_UNSUPPORTED": lambda d: replace(d, normalized_request_ref=reference()),
            "RESERVATION_MISMATCH": lambda d: replace(d, reserved_budget_draw={"rollouts": 1}),
            "NOT_APPROVED": lambda d: replace(d, decision="REQUIRE_APPROVAL"),
        }
        for code, change in variants.items():
            with self.subTest(code):
                self.setUp()
                original = self.gate.validate_request
                self.gate.validate_request = lambda *a, o=original, c=change: c(o(*a))
                self.run_script([ask("a"), finish()], approval=None)
                self.assertEqual(self.denial()[1], code)
                self.assertEqual(self.dispatched(), [])
        self.setUp()
        def broken(*args):
            raise RuntimeError("validator outage")
        self.gate.validate_request = broken
        self.run_script([ask("a"), finish()])
        self.assertEqual(self.dispatched(), [])
        rejection = [e for e in self.trace.read_events() if e["type"] == "EXECUTION"][0]
        self.assertIn("GATE_ERROR", rejection["output_summary"]["reason"])

    def test_consumer_require_approval_routes_read_to_approval_hook(self):
        original = self.gate.validate_request
        self.gate.validate_request = lambda *a: replace(original(*a), decision="REQUIRE_APPROVAL")
        self.run_script([ask("a"), finish()])
        self.assertEqual(self.dispatched(), ["a"])
        self.assertEqual(len(self.approval.seen), 1)

    def test_invalid_reference_revision_fails_closed(self):
        self.guard.revision = 1.5
        self.run_script([ask("a", "mutate"), finish()])
        self.assertEqual(self.denial()[1], "INVALID_REFERENCE_REVISION")

    # -- spec acceptance 12 + determinism ------------------------------------------------
    def test_state_update_bypasses_gates_and_tool_budget(self):
        update = ModelStateUpdateProposal("u1", 0, (StateDelta("SET_NOTE", "h", 1, "MODEL", 0),))
        result = self.run_script([scripted(state_update=update), finish()])
        self.assertEqual(self.gate_events(), [])
        self.assertEqual((result.budget_usage["tool_calls"], result.budget_usage["steps"]), (0, 1))

    def test_same_request_policy_state_budget_gives_identical_gate_trace(self):
        self.executor.extra = {"rollouts": 2, "horizon": 3}
        script = [ask("a", "sim"), ask("b", "mutate"), ask("c", "nope"), finish()]
        self.run_script(script)
        first = self.trace.events_path.read_bytes()
        self.setUp()
        self.executor.extra = {"rollouts": 2, "horizon": 3}
        self.run_script(script)
        self.assertEqual(first, self.trace.events_path.read_bytes())


class ReconcileTests(unittest.TestCase):
    budget = Budget(1, 1, 0, 0, 1, extra_dimensions={"r": 10})

    def test_under_draw_charges_actual_and_releases_remainder(self):
        result = reconcile({"r": 4}, {"r": 1}, self.budget)
        self.assertEqual((dict(result.charged), result.violations), ({"r": 1}, ()))

    def test_invalid_actual_draws_are_violations(self):
        for actual in (None, {"r": -1}, {"r": float("nan")}, {"r": True}, {"steps": 1}):
            with self.subTest(actual=actual):
                result = reconcile({"r": 4}, actual, self.budget)
                self.assertTrue(result.violations)
                self.assertEqual(result.charged["r"], 4)


if __name__ == "__main__":
    unittest.main()
