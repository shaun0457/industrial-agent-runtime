"""Offline acceptance checks for the unblocked B1 public state contracts."""

import ast
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from industrial_agent_runtime import (
    Budget, ContextProjection, InformationRef, ModelStateUpdateProposal,
    StateDelta, Task, TaskStateStore, TaskStatus, ToolResult, TraceEvent,
    TraceRecorder, Visibility, canonical_json, checksum, to_jsonable,
)


def reference(visibility=Visibility.AGENT):
    return InformationRef("input-1", "document", "fixture", "1", visibility,
                          "2026-09-17T00:00:00Z")


def projection(content=None):
    body = {"items": ["initial"]} if content is None else content
    return ContextProjection("p1", "t1", 0, body, (reference(),), "policy-v1",
                             8, checksum(body))


class ConsumerStore:
    """Test-owned store demonstrating the protocol without a runtime dependency."""

    def __init__(self):
        self.data = {}
        self.current = 0

    def revision(self):
        return self.current

    def status(self):
        return TaskStatus.RUNNING

    def project(self, policy):
        return replace(projection(self.data), base_revision=self.current)

    def apply_batch(self, deltas, expected_revision):
        if expected_revision != self.current:
            raise ValueError("stale")
        candidate = dict(self.data)
        for delta in deltas:
            if delta.operation != "SET_NOTE" or delta.target_ref_or_path == "policy":
                raise ValueError("illegal")
            if delta.proposed_base_revision != expected_revision:
                raise ValueError("stale delta")
            candidate[delta.target_ref_or_path] = delta.value_or_ref
        self.data = candidate
        self.current += 1
        return self.current


class ContractTests(unittest.TestCase):
    def test_serializable_task_preserves_extra_dimensions(self):
        budget = Budget(4, 3, 0, 0, 5, extra_dimensions={"resource_units": 9})
        task = Task("t1", "classify", (reference(),), ("read",), budget,
                    {"type": "object"})
        restored = json.loads(canonical_json(task))
        self.assertEqual(restored["budget"]["extra_dimensions"], {"resource_units": 9})
        self.assertEqual(restored["context_refs"][0]["visibility"], "AGENT")

    def test_nested_projection_is_an_immutable_snapshot(self):
        original = {"items": ["initial"]}
        item = projection(original)
        original["items"].append("changed")
        self.assertEqual(to_jsonable(item.content), {"items": ["initial"]})
        with self.assertRaises(TypeError):
            item.content["other"] = 1
        with self.assertRaises(AttributeError):
            item.content["items"].append("changed")

    def test_evaluator_ref_cannot_enter_projection(self):
        with self.assertRaises(ValueError):
            replace(projection(), included_refs=(reference(Visibility.EVALUATOR),))

    def test_invalid_visibility_fails_closed(self):
        with self.assertRaises(ValueError):
            reference("UNKNOWN")

    def test_invalid_budget_values_rejected(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Budget(value, 0, 0, 0, 0)
        for value in (-1, True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                Budget(1, 1, 0, 0, 1, extra_dimensions={"units": value})

    def test_json_rejects_objects_non_string_keys_and_nonfinite_numbers(self):
        for value in (object(), {1: "x"}, float("nan"), float("inf")):
            with self.subTest(value=repr(value)), self.assertRaises(TypeError):
                canonical_json(value)

    def test_model_delta_and_tool_result_snapshot_payloads(self):
        value = {"note": ["a"]}
        delta = StateDelta("SET_NOTE", "note", value, "MODEL", 0)
        proposal = ModelStateUpdateProposal("proposal1", 0, [delta])
        result = ToolResult("request1", "SUCCESS", value, {}, {"tool_version": "1"})
        value["note"].append("b")
        self.assertEqual(to_jsonable(proposal)["deltas"][0]["value_or_ref"], {"note": ["a"]})
        self.assertEqual(to_jsonable(result.structured_output), {"note": ["a"]})

    def test_consumer_protocol_atomic_multi_delta_success(self):
        store = ConsumerStore()
        self.assertIsInstance(store, TaskStateStore)
        deltas = (StateDelta("SET_NOTE", "a", 1, "MODEL", 0),
                  StateDelta("SET_NOTE", "b", 2, "MODEL", 0))
        self.assertEqual(store.apply_batch(deltas, 0), 1)
        self.assertEqual(store.data, {"a": 1, "b": 2})

    def test_consumer_protocol_illegal_batch_rolls_back_and_stale_rejects(self):
        store = ConsumerStore()
        with self.assertRaises(ValueError):
            store.apply_batch((StateDelta("SET_NOTE", "a", 1, "MODEL", 0),
                               StateDelta("SET_NOTE", "policy", {}, "MODEL", 0)), 0)
        self.assertEqual((store.data, store.revision()), ({}, 0))
        store.apply_batch((StateDelta("SET_NOTE", "a", 1, "MODEL", 0),), 0)
        with self.assertRaises(ValueError):
            store.apply_batch((StateDelta("SET_NOTE", "b", 2, "MODEL", 0),), 0)
        self.assertEqual((store.data, store.revision()), ({"a": 1}, 1))

    def test_runtime_imports_no_domain_or_framework_modules(self):
        source = Path(__file__).parents[1] / "src" / "industrial_agent_runtime"
        forbidden = {"tep_sim", "tep_agent_lab", "langgraph", "mcp", "openai", "anthropic"}
        for path in source.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = ([node.module] if isinstance(node, ast.ImportFrom)
                           else [item.name for item in node.names]
                           if isinstance(node, ast.Import) else [])
                for module in modules:
                    self.assertNotIn((module or "").split(".")[0], forbidden)


class TraceTests(unittest.TestCase):
    def model_event(self, ref):
        return TraceEvent(
            "e1", "t1", "MODEL_TURN", "2026-09-17T00:00:00Z", "RETURNED",
            {"model_calls": 1}, {"display": "summary"}, {"action": "NONE"},
            context_projection_ref=ref, prompt_template_version="prompt-v1",
            provider="fake", model="scripted", model_version="1",
            sampling_parameters={"temperature": 0}, registered_tool_set_version="tools-v1",
        )

    def test_exact_projection_and_metadata_survive_new_recorder(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = TraceRecorder(directory)
            item = projection()
            ref = recorder.persist_projection(item, "2026-09-17T00:00:00Z")
            recorder.append(self.model_event(ref))
            reopened = TraceRecorder(directory)
            self.assertEqual(reopened.read_projection(ref), to_jsonable(item))
            event = reopened.read_events()[0]
            self.assertEqual(event["context_projection_ref"]["checksum"], ref.checksum)
            self.assertEqual(event["sampling_parameters"], {"temperature": 0})
            self.assertEqual(event["registered_tool_set_version"], "tools-v1")

    def test_artifact_reuse_never_overwrites_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = TraceRecorder(directory)
            ref = recorder.persist_projection(projection(), "now")
            path = Path(directory) / ref.ref_id
            path.write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                recorder.persist_projection(projection(), "now")
            self.assertEqual(path.read_bytes(), b"corrupted")
            with self.assertRaises(ValueError):
                recorder.read_projection(ref)

    def test_model_event_requires_complete_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            ref = TraceRecorder(directory).persist_projection(projection(), "now")
            event = self.model_event(ref)
            for field in ("context_projection_ref", "prompt_template_version", "provider",
                          "model", "model_version", "registered_tool_set_version"):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    replace(event, **{field: None})

    def test_wrong_task_and_unknown_projection_never_append(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = TraceRecorder(directory)
            ref = recorder.persist_projection(projection(), "now")
            with self.assertRaises(ValueError):
                recorder.append(replace(self.model_event(ref), task_id="other"))
            with self.assertRaises(ValueError):
                recorder.append(self.model_event(replace(ref, ref_id="../../outside.json")))
            self.assertEqual(recorder.read_events(), [])

    def test_deterministic_artifact_and_event_bytes(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            recorders = (TraceRecorder(first), TraceRecorder(second))
            refs = [item.persist_projection(projection(), "now") for item in recorders]
            for recorder, ref in zip(recorders, refs):
                recorder.append(self.model_event(ref))
            self.assertEqual(refs[0], refs[1])
            self.assertEqual(recorders[0].events_path.read_bytes(),
                             recorders[1].events_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
