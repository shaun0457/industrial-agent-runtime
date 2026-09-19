"""B1 acceptance through deterministic consumer fixtures, with no domain imports."""

from dataclasses import replace
import tempfile
import unittest

from industrial_agent_runtime import (
    Action, Budget, ContextProjection, Coordinator, FakeProvider, FinishProposal,
    GateDecision, InformationRef, ModelStateUpdateProposal, ModelTurn, SideEffectClass,
    StateDelta, Subtask, Task, TaskStatus, ToolCallRequest, ToolResult, ToolSpec,
    TraceRecorder, Visibility, WorkBatch, WorkItem, checksum, to_jsonable,
)
from test_contracts import ConsumerStore, reference


def scripted(action=Action.NONE, **payload):
    def factory(projection, limits):
        return ModelTurn(f"turn-{limits['budget_usage']['model_calls']}",
                         limits["context_projection_ref"], projection.base_revision,
                         action, **payload)
    return factory


def finish(**payload):
    return scripted(Action.FINISH_PROPOSAL,
                    finish_proposal=FinishProposal({"answer": 7}, **payload))


def request(name="r1", tool="read", **arguments):
    return ToolCallRequest(name, tool, arguments)


def item(name, dependencies=(), **kwargs):
    return WorkItem(name, "TOOL", dependencies, request(name), **kwargs)


class Gate:
    """Trusted deterministic fixture: explicit schema + consumer request validation."""

    def __init__(self, store):
        self.store = store
        self.revisions = []
        self.allow = True

    def validate_request(self, request, spec, task, expected_state_revision, budget_usage):
        self.revisions.append(expected_state_revision)
        allowed = self.allow and set(request.arguments) <= {"note_key"}
        if "note_key" in request.arguments:
            allowed &= request.arguments["note_key"] in self.store.data
        return GateDecision(request.request_id, "ALLOW" if allowed else "DENY",
                            "consumer", "fixture", "fixture validator", "fixture-v1",
                            expected_state_revision=expected_state_revision)


class Execute:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.extra = {}
        self.refs = ()

    def execute(self, request, spec):
        self.calls.append(request)
        return ToolResult(request.request_id,
                          "FAILED" if request.request_id in self.fail else "SUCCESS",
                          {"value": request.request_id}, self.extra,
                          {"tool_version": "fixture-v1"}, information_refs=self.refs)


class Verify:
    def __init__(self):
        self.allow = True
        self.known_refs = set()

    def verify_result(self, result, request, spec, deltas, expected_state_revision):
        return (self.allow and result.provenance.get("tool_version") == "fixture-v1"
                and isinstance(result.structured_output.get("value"), str)
                and all(ref.ref_id in self.known_refs for ref in result.information_refs))

    def verify_finish(self, proposal, task, expected_state_revision):
        return (type(proposal.structured_output.get("answer")) is int and self.allow
                and all(ref.ref_id in self.known_refs for ref in
                        (*proposal.information_refs, *proposal.artifact_refs)))


class Ingest:
    def derive_deltas(self, result):
        # Deliberately stale originating revisions must be rebound by Coordinator.
        return (StateDelta("SET_NOTE", "result-" + result.request_id,
                           result.structured_output, "ignored", -99),)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConsumerStore()
        self.gate, self.executor, self.verifier = Gate(self.store), Execute(), Verify()
        self.trace = TraceRecorder(self.directory.name)
        self.task = Task("t1", "fixture objective", (), ("read",), Budget(10, 10, 0, 0, 20),
                         {"type": "object", "required": ["answer"]})
        self.spec = ToolSpec("read", "fixture read", {"type": "object"},
                             {"type": "object"}, SideEffectClass.READ)

    def run_script(self, turns, **overrides):
        self.provider = FakeProvider(turns)
        options = dict(model_metadata={"provider": "fake", "model": "scripted",
                        "model_version": "1", "prompt_template_version": "fixture-v1",
                        "sampling_parameters": {"temperature": 0}},
                       gate=self.gate, executor=self.executor, verifier=self.verifier,
                       ingestor=Ingest(), clock=lambda: "2026-09-18T00:00:00Z")
        options.update(overrides)
        self.coordinator = Coordinator(self.task, self.store, self.provider, self.trace,
                                       (self.spec,), **options)
        return self.coordinator.run()

    def test_no_tool_typed_finish_and_none(self):
        result = self.run_script([scripted(), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.structured_output["answer"], 7)
        self.assertEqual(result.budget_usage["model_calls"], 2)
        self.assertEqual(result.budget_usage["tool_calls"], 0)

    def test_atomic_update_precedes_same_turn_tool_validation(self):
        update = ModelStateUpdateProposal("u1", 0, (
            StateDelta("SET_NOTE", "hypothesis", {"claim": "x"}, "MODEL", 0),
            StateDelta("SET_NOTE", "question", "why", "MODEL", 0)))
        result = self.run_script([scripted(Action.TOOL_REQUEST, state_update=update,
                                           tool_request=request(note_key="hypothesis")), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.gate.revisions, [1])
        self.assertEqual(self.store.revision(), 3)  # proposal + ingestion + DONE
        self.assertEqual(result.budget_usage["steps"], 2)
        self.assertEqual(result.budget_usage["tool_calls"], 1)

    def test_rejected_atomic_update_suppresses_action_and_feedback_is_visible(self):
        update = ModelStateUpdateProposal("u1", 0, (
            StateDelta("SET_NOTE", "legal", 1, "MODEL", 0),
            StateDelta("SET_NOTE", "policy", {}, "MODEL", 0)))
        result = self.run_script([scripted(Action.TOOL_REQUEST, state_update=update,
                                           tool_request=request()), finish()])
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(result.budget_usage["steps"], 1)
        self.assertEqual(result.budget_usage["tool_calls"], 0)
        feedback = self.provider.projections[1].content["runtime_feedback"]
        self.assertEqual(feedback[0]["proposal_id"], "u1")
        self.assertEqual(feedback[0]["status"], "DENIED")

    def test_stale_update_suppresses_entire_work_batch(self):
        update = ModelStateUpdateProposal("stale", -1, (
            StateDelta("SET_NOTE", "note", 1, "MODEL", -1),))
        result = self.run_script([scripted(Action.WORK_BATCH, state_update=update,
            work_batch=WorkBatch("batch", "work", (item("a"),))), finish()])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(result.budget_usage["steps"], 1)
        self.assertEqual(self.store.revision(), 1)  # only the verified DONE transition

    def test_missing_delta_revision_suppresses_tool(self):
        update = ModelStateUpdateProposal("u1", 0, (StateDelta("SET_NOTE", "a", 1, "MODEL"),))
        self.run_script([scripted(Action.TOOL_REQUEST, state_update=update,
                                 tool_request=request()), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_state_changed_after_projection_rejects_proposal(self):
        def race(projection, limits):
            self.store.apply_batch((StateDelta("SET_NOTE", "concurrent", 1, "RESULT", 0),), 0)
            update = ModelStateUpdateProposal("u1", 0, (
                StateDelta("SET_NOTE", "model", 1, "MODEL", 0),))
            return scripted(Action.TOOL_REQUEST, state_update=update,
                            tool_request=request())(projection, limits)
        self.run_script([race, finish()])
        self.assertEqual(self.store.data, {"concurrent": 1})
        self.assertEqual(self.executor.calls, [])

    def test_model_cannot_impersonate_ingestion_producer(self):
        original = self.store.apply_batch
        producers = []
        def tracked(deltas, expected_revision):
            producers.extend(delta.producer for delta in deltas)
            return original(deltas, expected_revision)
        self.store.apply_batch = tracked
        update = ModelStateUpdateProposal("u1", 0, (
            StateDelta("SET_NOTE", "note", 1, "RUNTIME", 0),))
        self.run_script([scripted(state_update=update), finish()])
        self.assertEqual(producers, ["MODEL"])

    def test_update_batch_step_limit_stops_without_tool_dispatch(self):
        self.task = replace(self.task, budget=Budget(10, 10, 0, 0, 1))
        update = ModelStateUpdateProposal("u1", 0, (
            StateDelta("SET_NOTE", "a", 1, "MODEL", 0),))
        result = self.run_script([scripted(Action.TOOL_REQUEST, state_update=update,
                                           tool_request=request())])
        self.assertEqual(result.status, TaskStatus.EXHAUSTED)
        self.assertEqual(result.budget_usage["steps"], 1)
        self.assertEqual(self.executor.calls, [])

    def test_sequential_executor_collects_wave_then_ingests_lexical_current_revisions(self):
        batch = WorkBatch("batch", "work", (item("z"), item("a"), item("c", ("z",))))
        result = self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual([call.request_id for call in self.executor.calls], ["a", "z", "c"])
        self.assertEqual(self.gate.revisions, [0, 0, 2])
        events = [e for e in self.trace.read_events() if e["type"] == "RESULT_INGESTION"]
        self.assertEqual([e["input_summary"]["expected_revision"] for e in events], [0, 1, 2])
        self.assertEqual([e["work_id"] for e in events], ["a", "z", "c"])
        self.assertEqual(result.state_revision, 4)  # three ingestions + DONE
        self.assertEqual(result.budget_usage["tool_calls"], 3)

    def test_failed_dependency_skips_transitively_without_retry(self):
        self.executor.fail = {"a"}
        batch = WorkBatch("b", "work", (item("a"), item("b", ("a",)),
                                         item("c", ("b",)), item("d")))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual([call.request_id for call in self.executor.calls], ["a", "d"])
        terminal = {e["work_id"]: e["status"] for e in self.trace.read_events()
                    if e["type"] == "WORK_ITEM"}
        self.assertEqual(terminal, {"a": "FAILED", "b": "SKIPPED_DEPENDENCY",
                                    "c": "SKIPPED_DEPENDENCY", "d": "COMPLETED"})

    def test_cycle_unknown_dependency_duplicate_and_unknown_policy_all_fail_before_dispatch(self):
        batches = [WorkBatch("cycle", "", (item("a", ("b",)), item("b", ("a",)))),
                   WorkBatch("missing", "", (item("a", ("missing",)),)),
                   WorkBatch("duplicate", "", (item("a"), item("a"))),
                   WorkBatch("policy", "", (item("a"),), completion_policy="FIRST_SUCCESS")]
        self.run_script([*(scripted(Action.WORK_BATCH, work_batch=b) for b in batches), finish()])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(sum(e["type"] == "WORK_BATCH" and e["status"] == "DENIED"
                             for e in self.trace.read_events()), 4)

    def test_later_hidden_batch_ref_prevents_even_first_legal_item_dispatch(self):
        hidden = to_jsonable(reference(Visibility.EVALUATOR))
        later = WorkItem("z", "TOOL", (), request("z", nested={"refs": [hidden]}))
        batch = WorkBatch("b", "", (item("a"), later))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.gate.revisions, [])
        events = [e for e in self.trace.read_events() if e["type"] == "WORK_BATCH"]
        self.assertEqual(events[0]["status"], "DENIED")

    def test_batch_resource_and_cumulative_tool_budget_denials(self):
        self.task = replace(self.task, budget=Budget(10, 1, 0, 0, 20))
        batches = [WorkBatch("calls", "", (item("a"), item("b"))),
                   WorkBatch("resources", "", (item("a"),), budget_request={"resource": 1})]
        self.run_script([*(scripted(Action.WORK_BATCH, work_batch=b) for b in batches), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_subtasks_fail_closed_before_any_batch_tool(self):
        child = Subtask("child", "t1", "read", (), (), Budget(1, 0, 0, 0, 1), {}, "fixture")
        batch = WorkBatch("b", "", (item("a"), WorkItem("z", "SUBTASK", (), child)))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_unknown_tool_and_consumer_denial(self):
        self.gate.allow = False
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request(tool="unknown")),
                         scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_malformed_tool_arguments_denied_by_gate(self):
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request(unexpected=True)), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_missing_gate_hook_fails_closed(self):
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()], gate=None)
        self.assertEqual(self.executor.calls, [])

    def test_missing_verifier_cannot_finish_or_execute(self):
        self.task = replace(self.task, budget=Budget(2, 10, 0, 0, 20))
        result = self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()],
                                 verifier=None)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(result.status, TaskStatus.EXHAUSTED)

    def test_simulate_and_resource_declaring_tools_remain_disabled(self):
        self.spec = replace(self.spec, side_effect_class=SideEffectClass.SIMULATE,
                            declared_budget_draw={"rollouts": 1}, isolation_guarantee="sandbox")
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_unreserved_actual_draw_fails_verification_without_ingestion(self):
        self.executor.extra = {"resource": 1}
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(self.store.data, {})

    def test_missing_and_hidden_result_refs_do_not_ingest(self):
        self.executor.refs = (reference(),)
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(self.store.data, {})

    def test_hidden_result_ref_denied_even_when_verifier_recognizes_it(self):
        self.executor.refs = (reference(Visibility.EVALUATOR),)
        self.verifier.known_refs.add("input-1")
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(self.store.data, {})

    def test_result_verifier_denial_causes_dependency_skip(self):
        class RejectResult(Verify):
            def verify_result(self, *args):
                return False
        self.verifier = RejectResult()
        batch = WorkBatch("b", "", (item("a"), item("b", ("a",))))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual([call.request_id for call in self.executor.calls], ["a"])
        self.assertEqual(self.store.data, {})

    def test_gate_revision_mismatch_and_approval_never_dispatch(self):
        original = self.gate.validate_request
        def unbound(*args):
            return replace(original(*args), expected_state_revision=999)
        self.gate.validate_request = unbound
        self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(self.executor.calls, [])

    def test_tool_exception_is_failed_once_without_automatic_retry(self):
        def fail(call, spec):
            self.executor.calls.append(call)
            raise RuntimeError("fixture adapter failure")
        self.executor.execute = fail
        batch = WorkBatch("b", "", (item("a"), item("b", ("a",))))
        result = self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        self.assertEqual([call.request_id for call in self.executor.calls], ["a"])
        self.assertEqual(result.budget_usage["tool_calls"], 1)

    def test_token_budget_fails_before_provider_without_meter(self):
        self.task = replace(self.task, budget=replace(self.task.budget, max_total_tokens=100))
        result = self.run_script([finish()])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(self.provider.projections, [])

    def test_provider_exception_returns_failed_trace(self):
        result = self.run_script([])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.budget_usage["model_calls"], 1)
        model = [event for event in self.trace.read_events() if event["type"] == "MODEL_TURN"]
        self.assertEqual(len(model), 1)
        self.trace.read_projection(InformationRef(**model[0]["context_projection_ref"]))

    def test_zero_tool_budget_denies_before_gate_or_executor(self):
        self.task = replace(self.task, budget=replace(self.task.budget, max_tool_calls=0))
        result = self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request()), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(self.gate.revisions, [])
        self.assertEqual(self.executor.calls, [])

    def test_malformed_untyped_model_output_is_denied_then_can_replan(self):
        result = self.run_script([lambda projection, limits: {"action": "NONE"}, finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.budget_usage["model_calls"], 2)

    def test_missing_final_ref_rejected_then_explicit_retry_can_finish(self):
        result = self.run_script([finish(information_refs=(reference(),)), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.budget_usage["model_calls"], 2)

    def test_finish_verifier_exception_is_structured_failed_without_done(self):
        def broken(*args):
            raise RuntimeError("fixture verification failure")
        self.verifier.verify_finish = broken
        result = self.run_script([finish()])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIsNone(result.structured_output)
        self.assertEqual(self.executor.calls, [])
        failures = [e for e in self.trace.read_events() if e["type"] == "TASK_FAILURE"]
        self.assertEqual(failures[0]["output_summary"]["error"], "fixture verification failure")

    def test_hidden_final_ref_rejected_even_if_verifier_knows_it(self):
        self.verifier.known_refs.add("input-1")
        result = self.run_script([finish(information_refs=(reference(Visibility.EVALUATOR),)), finish()])
        self.assertEqual(result.budget_usage["model_calls"], 2)

    def test_model_turn_exact_projection_ref_and_state_update_trace(self):
        update = ModelStateUpdateProposal("u1", 0, (StateDelta("SET_NOTE", "a", 1, "MODEL", 0),))
        self.run_script([scripted(state_update=update), finish()])
        events = self.trace.read_events()
        model = next(e for e in events if e["type"] == "MODEL_TURN")
        ref = InformationRef(**model["context_projection_ref"])
        self.assertEqual(self.trace.read_projection(ref), to_jsonable(self.provider.projections[0]))
        state = next(e for e in events if e["type"] == "STATE_UPDATE")
        self.assertEqual(state["input_summary"]["proposal"]["proposal_id"], "u1")
        self.assertEqual(state["output_summary"]["resulting_revision"], 1)
        self.assertEqual(state["budget_delta"], {"steps": 1})

    def test_wrong_projection_binding_suppresses_execution(self):
        def wrong(projection, limits):
            return replace(scripted(Action.TOOL_REQUEST, tool_request=request())(projection, limits),
                           base_revision=999)
        self.run_script([wrong, finish()])
        self.assertEqual(self.executor.calls, [])

    def test_repeated_none_is_bounded_by_model_budget(self):
        self.task = replace(self.task, budget=Budget(2, 10, 0, 0, 20))
        result = self.run_script([scripted(), scripted()])
        self.assertEqual(result.status, TaskStatus.EXHAUSTED)
        self.assertEqual(result.budget_usage["model_calls"], 2)

    def test_trace_replay_is_identical_for_fixed_clock_provider_and_tools(self):
        script = [scripted(Action.WORK_BATCH, work_batch=WorkBatch("b", "", (item("z"), item("a")))),
                  finish()]
        self.run_script(script)
        first = self.trace.events_path.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            self.trace = TraceRecorder(directory)
            self.store = ConsumerStore()
            self.gate, self.executor, self.verifier = Gate(self.store), Execute(), Verify()
            self.run_script(script)
            self.assertEqual(first, self.trace.events_path.read_bytes())

    def test_verified_finish_persists_done_revision_and_trace_without_step_charge(self):
        result = self.run_script([finish()])
        self.assertEqual((result.status, self.store.status()), (TaskStatus.DONE, TaskStatus.DONE))
        self.assertEqual((result.state_revision, self.store.revision()), (1, 1))
        self.assertEqual(result.budget_usage["steps"], 0)
        event = next(e for e in self.trace.read_events() if e["type"] == "STATUS_TRANSITION")
        self.assertEqual(event["status"], "ACCEPTED")
        self.assertEqual(event["input_summary"]["expected_revision"], 0)
        self.assertEqual(event["output_summary"]["resulting_revision"], 1)
        self.assertEqual(event["output_summary"]["resulting_status"], "DONE")

    def test_exhaustion_persists_even_with_zero_remaining_work_budget(self):
        self.task = replace(self.task, budget=Budget(0, 0, 0, 0, 0))
        result = self.run_script([])
        self.assertEqual(result.status, TaskStatus.EXHAUSTED)
        self.assertEqual(self.store.status(), TaskStatus.EXHAUSTED)
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(result.budget_usage["steps"], 0)
        self.assertEqual(self.provider.projections, [])

    def test_provider_failure_persists_failed_status_and_revision(self):
        result = self.run_script([])
        self.assertEqual((result.status, self.store.status()), (TaskStatus.FAILED, TaskStatus.FAILED))
        self.assertEqual(result.state_revision, 1)
        self.assertIsNone(result.structured_output)

    def test_status_persistence_exception_clears_output_and_never_retries(self):
        calls = []
        def reject(status, expected_revision):
            calls.append((status, expected_revision))
            raise RuntimeError("fixture storage unavailable")
        self.store.transition_status = reject
        result = self.run_script([finish()])
        self.assertEqual(calls, [(TaskStatus.DONE, 0)])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIsNone(result.structured_output)
        self.assertEqual((self.store.status(), result.state_revision), (TaskStatus.RUNNING, 0))
        self.assertIn("STATUS_PERSISTENCE_FAILED", result.errors[0])
        event = next(e for e in self.trace.read_events() if e["type"] == "STATUS_TRANSITION")
        self.assertEqual(event["status"], "DENIED")
        self.assertEqual(event["output_summary"]["observed_status"], "RUNNING")
        self.assertEqual(event["output_summary"]["observed_revision"], 0)

    def test_status_persistence_wrong_return_revision_fails_closed_with_actual_state(self):
        original = self.store.transition_status
        def inconsistent(status, expected_revision):
            return original(status, expected_revision) + 1
        self.store.transition_status = inconsistent
        result = self.run_script([finish()])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(self.store.status(), TaskStatus.DONE)
        self.assertIsNone(result.structured_output)
        self.assertIn("inconsistent status/revision", result.errors[0])

    def test_status_persistence_wrong_status_fails_closed(self):
        def wrong_status(status, expected_revision):
            self.store.current += 1
            return self.store.current
        self.store.transition_status = wrong_status
        result = self.run_script([finish()])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(self.store.status(), TaskStatus.RUNNING)
        self.assertIsNone(result.structured_output)

    def test_status_change_without_new_revision_fails_closed(self):
        def no_revision(status, expected_revision):
            self.store.lifecycle = status
            return self.store.current
        self.store.transition_status = no_revision
        result = self.run_script([finish()])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.state_revision, 0)
        self.assertIn("revision advancement", result.errors[0])

    def test_already_cancelled_store_is_observed_without_provider_or_revision_change(self):
        self.store.transition_status(TaskStatus.CANCELLED, 0)
        result = self.run_script([])
        self.assertEqual(result.status, TaskStatus.CANCELLED)
        self.assertEqual(result.state_revision, 1)
        self.assertEqual(self.provider.projections, [])
        self.assertEqual(result.budget_usage["model_calls"], 0)

    def test_terminal_race_is_not_overwritten_or_retried(self):
        original = self.verifier.verify_finish
        def cancel_during_finish(*args):
            accepted = original(*args)
            self.store.transition_status(TaskStatus.CANCELLED, self.store.revision())
            return accepted
        self.verifier.verify_finish = cancel_during_finish
        result = self.run_script([finish()])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(self.store.status(), TaskStatus.CANCELLED)
        self.assertEqual(result.state_revision, 1)
        self.assertIsNone(result.structured_output)


class ActionContractTests(unittest.TestCase):
    def test_action_union_and_arguments_are_strict_and_immutable(self):
        with self.assertRaises(ValueError):
            ToolCallRequest("r", "read", [])
        with self.assertRaises(ValueError):
            ModelTurn("t", reference(), 0, Action.NONE, tool_request=request())
        with self.assertRaises(ValueError):
            ModelTurn("t", reference(), 0, Action.WORK_BATCH)
        payload = {"nested": [1]}
        call = ToolCallRequest("r", "read", payload)
        payload["nested"].append(2)
        self.assertEqual(to_jsonable(call.arguments), {"nested": [1]})


if __name__ == "__main__":
    unittest.main()
