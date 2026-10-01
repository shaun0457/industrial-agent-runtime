"""B3 deterministic post-execution verification and ordered result ingestion."""

from dataclasses import replace
import tempfile
import unittest

from industrial_agent_runtime import (
    Action, Budget, Coordinator, FakeProvider, FinishProposal, InformationRef, Reconciliation,
    ResultVerificationPipeline, StateDelta, Task, TaskStatus, ToolResult, ToolSpec,
    TraceRecorder, Visibility, VerificationRejected, VerificationStage, WorkBatch,
    SideEffectClass, ToolCallRequest, to_jsonable,
)
from test_contracts import ConsumerStore, reference
from test_coordinator import Execute, Ingest, Verify, finish, item, request, scripted
from test_gates import Consumer

OUTPUT_SCHEMA = {"type": "object", "required": ["value"],
                 "properties": {"value": {"type": "string"}}}
CLEAN = Reconciliation({}, {}, {}, ())


def ref(ref_id="obs-1", visibility=Visibility.AGENT, **fields):
    return replace(InformationRef(ref_id, "observation", "fixture", "1", visibility,
                                  "2026-10-01T00:00:00Z"), **fields)


class Harness(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConsumerStore()
        self.gate, self.executor, self.verifier = Consumer(self.store), Execute(), Verify()
        self.ingestor = Ingest()
        self.trace = TraceRecorder(self.directory.name)
        self.task = Task("t1", "fixture objective", (reference(Visibility.EVALUATOR),),
                         ("read",), Budget(10, 10, 0, 0, 20, extra_dimensions={"r": 10}),
                         {"type": "object", "required": ["answer"]})
        self.spec = ToolSpec("read", "fixture read", {"type": "object"}, OUTPUT_SCHEMA,
                             SideEffectClass.READ)

    def run_script(self, turns, **overrides):
        options = dict(model_metadata={"provider": "fake", "model": "scripted",
                                       "model_version": "1",
                                       "prompt_template_version": "fixture-v1"},
                       gate=self.gate, executor=self.executor, verifier=self.verifier,
                       ingestor=self.ingestor, clock=lambda: "2026-10-01T00:00:00Z")
        options.update(overrides)
        self.coordinator = Coordinator(self.task, self.store, FakeProvider(turns), self.trace,
                                       (self.spec,), **options)
        return self.coordinator.run()

    def events(self, kind):
        return [e for e in self.trace.read_events() if e["type"] == kind]

    def rejection(self):
        rejected = [e["output_summary"] for e in self.events("VERIFY_RESULT")
                    if e["status"] == "REJECTED"]
        return (rejected[-1]["stage"], rejected[-1]["reason_code"]) if rejected else None

    def returning(self, **changes):
        original = self.executor.execute

        def execute(call, spec):
            result = original(call, spec)
            return replace(result, **changes) if changes else result
        self.executor.execute = execute

    def one_call(self):
        return self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request("a")),
                                finish()])

    def assert_rejected(self, stage, code):
        self.one_call()
        self.assertEqual(self.rejection(), (stage, code))
        self.assertEqual(self.store.data, {})
        self.assertEqual(self.events("RESULT_INGESTION"), [])
        self.assertNotIn("obs-1", self.coordinator.known_refs)


class AcceptedPathTests(Harness):
    def test_verified_result_ingests_and_becomes_citable(self):
        self.executor.refs = (ref(),)
        self.verifier.known_refs.add("obs-1")
        cite = ToolCallRequest("b", "read", {"evidence": to_jsonable(ref())})
        result = self.run_script([scripted(Action.TOOL_REQUEST, tool_request=request("a")),
                                  scripted(Action.TOOL_REQUEST, tool_request=cite),
                                  finish(information_refs=(ref(),))])
        self.assertEqual(result.status, TaskStatus.DONE)
        accepted = self.events("VERIFY_RESULT")[0]["output_summary"]["decision"]
        self.assertEqual(accepted["decision"], "ACCEPT")
        self.assertEqual(accepted["passed_stages"], [
            "V0_IDENTITY", "V1_PROVENANCE", "V2_OUTPUT_SCHEMA", "V3_REFS", "V4_ACCOUNTING",
            "V5_INGESTION_STRUCTURE", "CONSUMER", "V6_REVISION"])
        self.assertEqual([c.request_id for c in self.executor.calls], ["a", "b"])
        self.assertEqual(self.events("VERIFY_FINISH")[0]["status"], "ACCEPTED")

    def test_ingestion_binds_current_revision_not_projection_revision(self):
        batch = WorkBatch("b", "", (item("z"), item("a"), item("m", ("a",))))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        ingested = self.events("RESULT_INGESTION")
        self.assertEqual([e["input_summary"]["expected_revision"] for e in ingested], [0, 1, 2])
        for event in ingested:
            for delta in event["input_summary"]["deltas"]:
                self.assertEqual(delta["producer"], "RESULT_INGESTION")
                self.assertEqual(delta["proposed_base_revision"],
                                 event["input_summary"]["expected_revision"])

    def test_wave_ingestion_order_is_trace_visible_and_stable(self):
        batch = WorkBatch("b", "", (item("z"), item("a"), item("m"), item("k", ("z",))))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        plans = [e["output_summary"] for e in self.events("INGESTION_ORDER")]
        self.assertEqual(plans, [
            {"policy": "work_id_lexical_per_wave", "wave": 1, "order": ["a", "m", "z"]},
            {"policy": "work_id_lexical_per_wave", "wave": 2, "order": ["k"]}])
        ingested = self.events("RESULT_INGESTION")
        self.assertEqual([(e["work_id"], e["input_summary"]["sequence"]) for e in ingested],
                         [("a", 0), ("m", 1), ("z", 2), ("k", 0)])

    def test_identical_runs_produce_identical_verification_traces(self):
        def run(directory):
            self.setUp()
            self.trace = TraceRecorder(directory)
            self.executor.refs = (ref(),)
            self.verifier.known_refs.add("obs-1")
            batch = WorkBatch("b", "", (item("z"), item("a", ("z",))))
            self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
            return [(e["type"], e["status"], e.get("work_id"), e["output_summary"])
                    for e in self.trace.read_events()
                    if e["type"] in ("VERIFY_RESULT", "INGESTION_ORDER", "RESULT_INGESTION")]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertEqual(run(first), run(second))


class IdentityAndProvenanceTests(Harness):
    def test_misbound_result_is_rejected_and_charged_full_reservation(self):
        self.spec = replace(self.spec, declared_budget_draw={"r": 3})
        self.executor.extra = {"r": 1}
        self.returning(request_id="other")
        self.assert_rejected("V0_IDENTITY", "MISBOUND_RESULT")
        self.assertEqual(self.coordinator.usage["r"], 3)

    def test_untyped_result_is_rejected(self):
        self.executor.execute = lambda call, spec: {"request_id": call.request_id,
                                                    "status": "SUCCESS"}
        self.assert_rejected("V0_IDENTITY", "UNTYPED_RESULT")

    def test_unsuccessful_status_is_not_ingested(self):
        self.executor.fail = {"a"}
        self.assert_rejected("V0_IDENTITY", "TOOL_NOT_SUCCEEDED")

    def test_missing_tool_version_or_mismatched_provenance_is_rejected(self):
        cases = {"MISSING_PROVENANCE_FIELD": {}, "PROVENANCE_MISMATCH": {
            "tool_version": "fixture-v1", "request_id": "other"}}
        for code, provenance in cases.items():
            with self.subTest(code):
                self.setUp()
                self.returning(provenance=provenance)
                self.verifier.verify_result = lambda *args: True
                self.assert_rejected("V1_PROVENANCE", code)
        self.setUp()
        self.returning(provenance={"tool_version": "v", "tool_name": "other-tool"})
        self.verifier.verify_result = lambda *args: True
        self.assert_rejected("V1_PROVENANCE", "PROVENANCE_MISMATCH")

    def test_output_schema_violation_is_rejected(self):
        self.returning(structured_output={"value": 7})
        self.verifier.verify_result = lambda *args: True
        self.assert_rejected("V2_OUTPUT_SCHEMA", "INVALID_OUTPUT")


class RefAndVisibilityTests(Harness):
    def setUp(self):
        super().setUp()
        self.verifier.verify_result = lambda *args: True  # isolate runtime-owned checks

    def test_hidden_or_malformed_result_refs_are_rejected(self):
        cases = {
            "HIDDEN_REF": (ref(visibility=Visibility.EVALUATOR),),
            "MALFORMED_REF": ({"ref_id": "obs-1", "visibility": "AGENT"},),
            "DUPLICATE_REF_ID": (ref(), ref(version="2")),
            # A hidden task context ref id cannot be re-published as AGENT-visible.
            "REF_ID_CONFLICT": (reference(),),
        }
        for code, refs in cases.items():
            with self.subTest(code):
                self.setUp()
                self.executor.refs = refs
                self.assert_rejected("V3_REFS", code)

    def test_hidden_or_undeclared_refs_embedded_in_output_are_rejected(self):
        hidden = dict(to_jsonable(ref()), visibility="EVALUATOR")
        self.spec = replace(self.spec, output_schema={"type": "object"})
        self.returning(structured_output={"value": "x", "truth": hidden})
        self.assert_rejected("V3_REFS", "HIDDEN_REF_IN_OUTPUT")
        self.setUp()
        self.spec = replace(self.spec, output_schema={"type": "object"})
        self.returning(structured_output={"value": "x", "cite": to_jsonable(ref("ghost"))})
        self.assert_rejected("V3_REFS", "UNDECLARED_REF_IN_OUTPUT")

    def test_rejected_result_ref_is_never_citable_later(self):
        self.executor.refs = (ref(visibility=Visibility.EVALUATOR),)
        cite = ToolCallRequest("b", "read", {"evidence": to_jsonable(ref())})
        result = self.run_script([
            scripted(Action.TOOL_REQUEST, tool_request=request("a")),
            scripted(Action.TOOL_REQUEST, tool_request=cite),
            finish(information_refs=(ref(),)), finish()])
        self.assertEqual([c.request_id for c in self.executor.calls], ["a"])
        gate = [e["output_summary"] for e in self.events("GATE")]
        self.assertEqual(gate[-1]["reason_code"], "UNKNOWN_REF")
        finish_rejection = self.events("VERIFY_FINISH")[0]["output_summary"]
        self.assertEqual(finish_rejection["reason_code"], "UNKNOWN_REF")
        self.assertEqual(result.status, TaskStatus.DONE)  # explicit retry without the ref


class AccountingAndIngestionTests(Harness):
    def test_overdraw_is_a_structured_verification_failure(self):
        self.spec = replace(self.spec, declared_budget_draw={"r": 2})
        self.executor.extra = {"r": 5}
        self.assert_rejected("V4_ACCOUNTING", "RECONCILIATION_VIOLATION")
        self.assertEqual(self.coordinator.usage["r"], 5)  # charged actual, not hidden

    def test_ingestor_failures_and_malformed_deltas_are_rejected(self):
        def boom(result):
            raise RuntimeError("fixture ingestor failure")
        cases = {
            "INGESTOR_ERROR": boom,
            "MALFORMED_DELTA": lambda result: ({"operation": "SET_NOTE"},),
            "HIDDEN_REASON_REF": lambda result: (StateDelta(
                "SET_NOTE", "x", 1, "RESULT_INGESTION", 0,
                ref(visibility=Visibility.EVALUATOR)),),
        }
        for code, derive in cases.items():
            with self.subTest(code):
                self.setUp()
                self.ingestor.derive_deltas = derive
                self.assert_rejected("V5_INGESTION_STRUCTURE", code)

    def test_consumer_verdict_must_be_exactly_true(self):
        def boom(*args):
            raise RuntimeError("fixture verifier failure")
        for code, verdict in (("CONSUMER_REJECTED", lambda *a: False),
                              ("CONSUMER_REJECTED", lambda *a: "PASSED"),
                              ("VERIFIER_ERROR", boom)):
            with self.subTest(code):
                self.setUp()
                self.verifier.verify_result = verdict
                self.assert_rejected("CONSUMER", code)

    def test_state_change_during_verification_is_rejected(self):
        def mutate(*args):
            self.store.current += 1
            return True
        self.verifier.verify_result = mutate
        self.assert_rejected("V6_REVISION", "STATE_CHANGED")

    def test_consumer_apply_rejection_after_verification_keeps_refs_uncitable(self):
        self.executor.refs = (ref(),)
        self.verifier.known_refs.add("obs-1")

        def reject(deltas, expected_revision):
            raise ValueError("fixture store rejects batch")
        self.store.apply_batch = reject
        self.one_call()
        self.assertEqual(self.events("RESULT_INGESTION")[0]["status"], "REJECTED")
        self.assertNotIn("obs-1", self.coordinator.known_refs)

    def test_inconsistent_apply_revision_fails_the_run_closed(self):
        original = self.store.apply_batch
        self.store.apply_batch = lambda deltas, expected_revision: (
            original(deltas, expected_revision) + 5)
        result = self.one_call()
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("apply_batch returned a revision", result.errors[0])

    def test_failed_verification_skips_dependents_in_work_batch(self):
        self.verifier.verify_result = lambda *args: False
        batch = WorkBatch("b", "", (item("a"), item("b", ("a",))))
        self.run_script([scripted(Action.WORK_BATCH, work_batch=batch), finish()])
        outcomes = {e["work_id"]: e["status"] for e in self.events("WORK_ITEM")}
        self.assertEqual(outcomes, {"a": "FAILED", "b": "SKIPPED_DEPENDENCY"})


class FinishTests(Harness):
    def test_final_output_schema_and_unknown_refs_are_rejected(self):
        result = self.run_script([
            scripted(Action.FINISH_PROPOSAL, finish_proposal=FinishProposal({"wrong": 1})),
            finish(information_refs=(ref("nowhere"),)), finish()])
        codes = [e["output_summary"]["reason_code"] for e in self.events("VERIFY_FINISH")
                 if e["status"] == "REJECTED"]
        self.assertEqual(codes, ["INVALID_FINAL_OUTPUT", "UNKNOWN_REF"])
        self.assertEqual(result.status, TaskStatus.DONE)


class PipelineUnitTests(unittest.TestCase):
    def test_pipeline_is_deterministic_and_takes_no_model_input(self):
        verifier, ingestor = Verify(), Ingest()
        pipeline = ResultVerificationPipeline(verifier, ingestor)
        spec = ToolSpec("read", "", {"type": "object"}, OUTPUT_SCHEMA, SideEffectClass.READ)
        call = ToolCallRequest("a", "read", {})
        result = ToolResult("a", "SUCCESS", {"value": "a"}, {}, {"tool_version": "fixture-v1"})
        first = pipeline.verify(result, call, spec, CLEAN, {}, {}, 3, lambda: 3)
        self.assertEqual(first, pipeline.verify(result, call, spec, CLEAN, {}, {}, 3, lambda: 3))
        self.assertEqual(first.deltas[0].proposed_base_revision, 3)
        self.assertNotIn("G0", " ".join(stage.value for stage in VerificationStage))
        with self.assertRaises(VerificationRejected) as rejected:
            pipeline.verify(replace(result, status="SUCCESS (verified by model)"), call, spec,
                            CLEAN, {}, {}, 3, lambda: 3)
        self.assertEqual(rejected.exception.decision.reason_code, "TOOL_NOT_SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
