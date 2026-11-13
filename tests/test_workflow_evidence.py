import unittest

from hub.domain.workflow_evidence import (
    ValidationEvidence, can_release_successor, delivery_state, validation_state,
)

CURRENT = "a" * 64
OLD = "b" * 64


class WorkflowEvidenceTests(unittest.TestCase):
    def test_old_subject_and_old_validator_revision_cannot_pass(self) -> None:
        evidence = (
            ValidationEvidence("old-source", "tests@1", OLD, "passed"),
            ValidationEvidence("old-recipe", "tests@0", CURRENT, "passed"),
        )
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), evidence), "pending")

    def test_all_current_required_validators_must_pass(self) -> None:
        evidence = (
            ValidationEvidence("review", "review@1", CURRENT, "passed"),
            ValidationEvidence("tests", "tests@1", CURRENT, "passed"),
        )
        required = frozenset({"review@1", "tests@1"})
        self.assertEqual(validation_state(CURRENT, required, evidence), "passed")
        self.assertEqual(validation_state(CURRENT, required, evidence[:1]), "pending")

    def test_failure_and_inconclusive_have_explicit_results(self) -> None:
        failed = (ValidationEvidence("bad", "tests@1", CURRENT, "failed"),)
        unsure = (ValidationEvidence("unsure", "tests@1", CURRENT, "inconclusive"),)
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), failed), "failed")
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), unsure), "inconclusive")
        self.assertFalse(can_release_successor("succeeded", "inconclusive", validation_required=True))

    def test_same_evidence_key_cannot_change_content(self) -> None:
        good = ValidationEvidence("one", "tests@1", CURRENT, "passed")
        bad = ValidationEvidence("one", "tests@1", CURRENT, "failed")
        self.assertEqual(validation_state(CURRENT, frozenset({"tests@1"}), (good, good)), "passed")
        with self.assertRaises(ValueError):
            validation_state(CURRENT, frozenset({"tests@1"}), (good, bad))

    def test_not_requested_is_not_a_pass(self) -> None:
        self.assertEqual(validation_state(CURRENT, frozenset(), ()), "not_requested")
        self.assertTrue(can_release_successor("succeeded", "not_requested", validation_required=False))
        self.assertFalse(can_release_successor("succeeded", "not_requested", validation_required=True))
        self.assertFalse(can_release_successor("running", "passed", validation_required=True))

    def test_execution_completion_still_requires_acceptance(self) -> None:
        self.assertEqual(delivery_state(
            "succeeded", "passed", "pending", validation_required=True, acceptance_required=True,
        ), "awaiting_acceptance")
        self.assertEqual(delivery_state(
            "succeeded", "passed", "accepted", validation_required=True, acceptance_required=True,
        ), "delivered")

    def test_acceptance_cannot_override_failed_or_missing_validation(self) -> None:
        for validation, expected in (("failed", "validation_failed"), ("pending", "awaiting_validation")):
            self.assertEqual(delivery_state(
                "succeeded", validation, "accepted", validation_required=True, acceptance_required=True,
            ), expected)

    def test_execution_and_rejection_priorities(self) -> None:
        for execution, expected in (
            ("unknown", "needs_reconciliation"), ("failed", "execution_failed"),
            ("cancelled", "cancelled"), ("waiting_external", "in_progress"),
        ):
            self.assertEqual(delivery_state(
                execution, "passed", "accepted", validation_required=True, acceptance_required=True,
            ), expected)
        self.assertEqual(delivery_state(
            "succeeded", "passed", "rejected", validation_required=True, acceptance_required=True,
        ), "rejected")

    def test_invalid_evidence_and_boolean_flags_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ValidationEvidence("bad", "tests@1", "not-a-hash", "passed")
        with self.assertRaises(ValueError):
            ValidationEvidence("bad", "tests@1", CURRENT, "success")
        with self.assertRaises(ValueError):
            delivery_state("succeeded", "passed", "accepted", validation_required=1, acceptance_required=True)
        with self.assertRaises(ValueError):
            validation_state(CURRENT, frozenset({"tests@1"}), tuple(
                ValidationEvidence(str(i), "tests@1", CURRENT, "passed") for i in range(33)
            ))


if __name__ == "__main__":
    unittest.main()
