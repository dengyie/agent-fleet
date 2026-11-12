import unittest

from hub.domain.workflow import transition_step


class WorkflowStateTests(unittest.TestCase):
    def test_bound_execution_can_complete_while_coordinator_is_waiting(self) -> None:
        for state in ("waiting_node", "waiting_external", "waiting_approval", "paused"):
            with self.subTest(state=state):
                self.assertEqual(transition_step(state, "succeeded"), "succeeded")

    def test_normal_execution_path(self) -> None:
        state = "pending"
        for target in ("ready", "dispatching", "running", "succeeded"):
            state = transition_step(state, target)
        self.assertEqual(state, "succeeded")

    def test_wait_resume_and_idempotent_projection(self) -> None:
        self.assertEqual(transition_step("ready", "waiting_node"), "waiting_node")
        self.assertEqual(transition_step("waiting_node", "ready"), "ready")
        self.assertEqual(transition_step("running", "waiting_external"), "waiting_external")
        self.assertEqual(transition_step("succeeded", "succeeded"), "succeeded")

    def test_unknown_requires_explicit_reconciliation(self) -> None:
        with self.assertRaises(ValueError):
            transition_step("unknown", "succeeded")
        self.assertEqual(
            transition_step("unknown", "succeeded", reconciled=True), "succeeded",
        )
        with self.assertRaises(ValueError):
            transition_step("unknown", "running", reconciled=True)

    def test_dispatched_cancellation_requires_confirmed_boundary(self) -> None:
        self.assertEqual(transition_step("pending", "cancelled"), "cancelled")
        with self.assertRaises(ValueError):
            transition_step("running", "cancelled")
        self.assertEqual(transition_step("running", "cancelling"), "cancelling")
        self.assertEqual(transition_step("cancelling", "unknown"), "unknown")
        self.assertEqual(transition_step("cancelling", "cancelled"), "cancelled")
        self.assertEqual(transition_step("cancelling", "succeeded"), "succeeded")

    def test_terminal_and_required_steps_cannot_be_reopened_or_skipped(self) -> None:
        for current, target in (
            ("succeeded", "ready"), ("failed", "running"),
            ("pending", "skipped"), ("pending", "succeeded"),
        ):
            with self.subTest(current=current, target=target), self.assertRaises(ValueError):
                transition_step(current, target)

    def test_bad_states_and_reconciliation_flags_are_rejected(self) -> None:
        for current, target, flag in (
            (True, "ready", False), ("ready", {}, False),
            ("queued", "ready", False), ("ready", "running", 1),
            ("running", "succeeded", True),
        ):
            with self.subTest(current=current, target=target), self.assertRaises(ValueError):
                transition_step(current, target, reconciled=flag)


if __name__ == "__main__":
    unittest.main()
