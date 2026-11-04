"""B5.1: what a real provider can report or raise, driven through Coordinator.run()."""

from dataclasses import replace
import unittest

from industrial_agent_runtime import (Budget, ModelUsage, ProviderOutputError,
                                      ProviderResponse, ProviderTransportError, TaskStatus)
import test_coordinator  # module import, so the loader does not collect B1 tests twice
from test_coordinator import finish, scripted


SECRET = "nvapi-sentinel-0123456789"


def usage_of(attempts=1, **fields):
    fields.setdefault("retry_reasons", ("PROVIDER_UNAVAILABLE",) * (attempts - 1))
    return ModelUsage(attempts=attempts, prompt_tokens=100, completion_tokens=20,
                      total_tokens=120, finish_reason="stop", latency_seconds=1.5,
                      **fields)


def with_usage(factory, attempts=1, **fields):
    def respond(projection, limits):
        return ProviderResponse(factory(projection, limits), usage_of(attempts, **fields))
    return respond


def raising(error):
    def respond(projection, limits):
        raise error
    return respond


def malformed(code):
    return raising(ProviderOutputError(code))


class ProviderResponseTests(unittest.TestCase):
    # Reuse the B1 fixture assembly without re-running the B1 tests themselves.
    setUp = test_coordinator.CoordinatorTests.setUp
    run_script = test_coordinator.CoordinatorTests.run_script

    def events(self, kind):
        return [event for event in self.trace.read_events() if event["type"] == kind]

    def test_usage_is_traced_on_a_model_usage_event(self):
        result = self.run_script([with_usage(finish())])
        self.assertEqual(result.status, TaskStatus.DONE)
        [usage] = self.events("MODEL_USAGE")
        self.assertEqual(usage["output_summary"]["attempts"], 1)
        self.assertEqual(usage["output_summary"]["total_tokens"], 120)
        self.assertEqual(usage["latency"], 1.5)

    def test_retry_attempts_are_charged_to_model_calls(self):
        result = self.run_script([with_usage(scripted(), attempts=3,
                                             retry_reasons=("PROVIDER_RATE_LIMITED",) * 2),
                                  finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.budget_usage["model_calls"], 4)  # 3 attempts + 1 call
        [usage] = self.events("MODEL_USAGE")
        self.assertEqual(usage["budget_delta"], {"model_calls": 2})
        self.assertEqual(usage["output_summary"]["retry_reasons"],
                         ["PROVIDER_RATE_LIMITED", "PROVIDER_RATE_LIMITED"])

    def test_provider_sees_remaining_budget_and_may_use_it_exactly(self):
        self.task = replace(self.task, budget=Budget(3, 10, 0, 0, 20))
        seen = []

        def spend_all(projection, limits):
            seen.append((limits["remaining_model_calls"], limits["remaining_total_tokens"]))
            return with_usage(scripted(), attempts=3)(projection, limits)

        result = self.run_script([spend_all])
        self.assertEqual(seen, [(2, None)])  # after this call's own charge; unmetered
        self.assertEqual(result.status, TaskStatus.EXHAUSTED)
        self.assertEqual(result.budget_usage["model_calls"], 3)

    def test_attempts_beyond_the_remaining_budget_fail_the_run(self):
        self.task = replace(self.task, budget=Budget(3, 10, 0, 0, 20))
        result = self.run_script([with_usage(finish(), attempts=4)])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIsNone(result.structured_output)
        self.assertEqual(result.errors, ("PROVIDER_USAGE_OVER_BUDGET: provider reported "
                                         "more attempts than the model-call budget allowed",))
        self.assertEqual(self.events("FINISH"), [])
        # What the provider says it spent is still recorded, even beyond the limit.
        [usage] = self.events("MODEL_USAGE")
        self.assertEqual(usage["status"], "OVER_BUDGET")
        self.assertEqual(result.budget_usage["model_calls"], 4)

    def test_usage_is_traced_between_the_request_and_the_output(self):
        self.run_script([with_usage(finish())])
        kinds = [event["type"] for event in self.trace.read_events()
                 if event["type"].startswith("MODEL_")]
        self.assertEqual(kinds, ["MODEL_TURN", "MODEL_USAGE", "MODEL_OUTPUT"])

    def test_retries_before_a_malformed_reply_are_charged_and_traced(self):
        error = ProviderOutputError("OUTPUT_NOT_JSON", usage=usage_of(2))
        result = self.run_script([raising(error), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.budget_usage["model_calls"], 3)
        self.assertEqual(len(self.events("MODEL_USAGE")), 1)
        self.assertEqual(len(self.events("MODEL_REJECTION")), 1)

    def test_retries_before_a_transport_failure_are_charged_and_traced(self):
        error = ProviderTransportError("PROVIDER_TIMEOUT", usage=usage_of(3))
        result = self.run_script([raising(error)])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.errors, ("PROVIDER_TIMEOUT",))
        self.assertEqual(result.budget_usage["model_calls"], 3)
        self.assertEqual(len(self.events("MODEL_USAGE")), 1)

    def test_error_subclass_text_never_reaches_results_or_trace(self):
        class Chatty(ProviderTransportError):
            def __str__(self):
                return f"PROVIDER_TIMEOUT: upstream said {SECRET}"

        class Forged(ProviderOutputError):
            @property
            def code(self):
                return f"OUTPUT: {SECRET}"

        for error, status, expected in (
                (Chatty("PROVIDER_TIMEOUT"), TaskStatus.FAILED, ("PROVIDER_TIMEOUT",)),
                (Forged("OUTPUT_SHAPE"), TaskStatus.FAILED,
                 ("PROVIDER_ERROR: provider raised Forged",))):
            with self.subTest(type(error).__name__):
                self.setUp()
                result = self.run_script([raising(error), finish()])
                self.assertEqual(result.status, status)
                self.assertEqual(result.errors, expected)
                self.assertNotIn(SECRET, self.trace.events_path.read_text(encoding="utf-8"))

    def test_error_code_is_read_only(self):
        error = ProviderOutputError("OUTPUT_SHAPE")
        with self.assertRaises(AttributeError):
            error.code = SECRET

    def test_exhausted_fake_script_names_the_cause(self):
        result = self.run_script([])
        self.assertEqual(result.errors, ("FAKE_PROVIDER_EXHAUSTED",))

    def test_usage_carries_codes_not_provider_text(self):
        invalid = (
            dict(attempts=2, retry_reasons=(f"429 for key {SECRET}",)),
            dict(attempts=4, retry_reasons="ABC"),  # a bare string is not a list of codes
            dict(attempts=1, retry_reasons=("PROVIDER_TIMEOUT",)),  # one reason per retry
            dict(attempts=1, finish_reason=f"stop {SECRET}"),
            dict(attempts=0), dict(attempts=True),
            dict(attempts=1, prompt_tokens=-1), dict(attempts=1, total_tokens=1.5),
            dict(attempts=1, latency_seconds=float("nan")),
        )
        for fields in invalid:
            with self.subTest(fields), self.assertRaises(ValueError):
                ModelUsage(**fields)
        usage = ModelUsage(attempts=2, retry_reasons=["PROVIDER_RATE_LIMITED"],
                           finish_reason="length")
        self.assertEqual(usage.retry_reasons, ("PROVIDER_RATE_LIMITED",))
        self.assertIsNone(usage.total_tokens)  # unreported stays None, never zero

    def test_malformed_output_is_a_rejected_turn_with_feedback(self):
        result = self.run_script([malformed("OUTPUT_NOT_JSON"), finish()])
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.budget_usage["model_calls"], 2)
        [rejection] = self.events("MODEL_REJECTION")
        self.assertEqual(rejection["output_summary"]["reason"], "OUTPUT_NOT_JSON")
        feedback = self.provider.projections[1].content["runtime_feedback"]
        self.assertEqual([entry["reason"] for entry in feedback], ["OUTPUT_NOT_JSON"])

    def test_repeated_malformed_output_exhausts_and_never_finishes(self):
        self.task = replace(self.task, budget=Budget(3, 10, 0, 0, 20))
        result = self.run_script([malformed("OUTPUT_TRUNCATED")] * 3 + [finish()])
        self.assertEqual(result.status, TaskStatus.EXHAUSTED)
        self.assertIsNone(result.structured_output)
        self.assertEqual(len(self.events("MODEL_REJECTION")), 3)
        self.assertEqual(self.events("FINISH"), [])

    def test_transport_error_fails_the_run_with_the_code_only(self):
        result = self.run_script([raising(ProviderTransportError("PROVIDER_TIMEOUT"))])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.errors, ("PROVIDER_TIMEOUT",))
        self.assertEqual(self.events("MODEL_OUTPUT"), [])

    def test_unexpected_provider_exception_is_recorded_by_type_not_text(self):
        result = self.run_script([raising(RuntimeError(f"HTTP 500: echo {SECRET}"))])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.errors, ("PROVIDER_ERROR: provider raised RuntimeError",))
        self.assertNotIn(SECRET, self.trace.events_path.read_text(encoding="utf-8"))

    def test_finish_verifier_exception_never_finishes(self):
        def boom(*args):
            raise RuntimeError("fixture verifier failure")
        self.verifier.verify_finish = boom
        result = self.run_script([with_usage(finish())])
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIsNone(result.structured_output)
        self.assertEqual(self.events("FINISH"), [])

    def test_output_error_codes_cannot_carry_free_text(self):
        for bad in ("", "lower_case", "OUTPUT: upstream said nvapi-secret"):
            with self.assertRaises(ValueError):
                ProviderOutputError(bad)


if __name__ == "__main__":
    unittest.main()
