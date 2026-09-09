import tempfile
import threading
import time
import unittest
from pathlib import Path

from hub import task_store


class TaskStoreTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_db = task_store.DB_PATH
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()

    def tearDown(self):
        task_store.DB_PATH = self.old_db

    def _create(self, **kw):
        params = dict(machine="mac-local", agent_type="codex", project="agent-fleet",
                      instruction="修复测试", requested_by="op@example.com")
        params.update(kw)
        return task_store.create_task(**params)


class TaskCreateTests(TaskStoreTestBase):
    def test_create_task_returns_queued_row(self):
        task, created = self._create()
        self.assertTrue(created)
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["machine"], "mac-local")
        self.assertTrue(task["task_id"].startswith("t-"))
        self.assertTrue(task["attempt_id"])

    def test_client_token_makes_create_idempotent(self):
        first, created1 = self._create(client_token="web-abc")
        second, created2 = self._create(client_token="web-abc")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first["task_id"], second["task_id"])

    def test_get_task_and_list_tasks(self):
        task, _ = self._create()
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["instruction"], "修复测试")
        self.assertIsNone(got["result"])
        self.assertIsNone(task_store.get_task("t-missing"))
        listing = task_store.list_tasks(machine="mac-local")
        self.assertEqual([t["task_id"] for t in listing], [task["task_id"]])
        self.assertEqual(task_store.list_tasks(machine="other"), [])
        self.assertEqual(task_store.list_tasks(state="failed"), [])

    def test_corrupt_db_is_quarantined_and_rebuilt(self):
        task_store.DB_PATH.write_bytes(b"not a sqlite database at all")
        task_store.init_db()  # 不抛异常
        task, created = self._create()
        self.assertTrue(created)
        self.assertTrue((self.temp_dir / "fleet.db.corrupt").exists())

    def test_oversized_instruction_is_truncated_to_2000(self):
        task, created = self._create(instruction="x" * 5000)
        self.assertTrue(created)
        self.assertEqual(len(task["instruction"]), 2000)


class LeaseTests(TaskStoreTestBase):
    def test_lease_task_moves_queued_to_leased(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="runner-1")
        self.assertEqual(lease["task_id"], task["task_id"])
        self.assertEqual(lease["instruction"], "修复测试")
        self.assertTrue(lease["nonce"])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "leased")
        # 已被领走，再次领取为 None
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r2"))

    def test_lease_is_scoped_to_machine(self):
        self._create()
        self.assertIsNone(task_store.lease_task(machine="other-machine", runner_id="r1"))

    def test_concurrent_lease_only_one_wins(self):
        task, _ = self._create()
        winners = []

        def grab(rid):
            lease = task_store.lease_task(machine="mac-local", runner_id=rid)
            if lease:
                winners.append(rid)

        threads = [threading.Thread(target=grab, args=(f"r{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)

    def test_heartbeat_extends_and_marks_running(self):
        self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        t0 = time.time()
        out = task_store.heartbeat(attempt_id=lease["attempt_id"], nonce=lease["nonce"], now=t0)
        self.assertEqual(out["task_id"], lease["task_id"])
        self.assertEqual(task_store.get_task(lease["task_id"])["state"], "running")
        # 错误 nonce 拒绝
        self.assertIsNone(task_store.heartbeat(attempt_id=lease["attempt_id"], nonce="wrong"))

    def test_expired_lease_requeues_task(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        t_future = time.time() + 301
        requeued = task_store.expire_leases(now=t_future)
        self.assertEqual(requeued, [task["task_id"]])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "queued")
        # 过期后心跳失败
        self.assertIsNone(task_store.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], now=t_future))
        # 重派后可被再次领取
        lease2 = task_store.lease_task(machine="mac-local", runner_id="r2", now=t_future + 1)
        self.assertEqual(lease2["task_id"], task["task_id"])
        self.assertNotEqual(lease2["attempt_id"], lease["attempt_id"])

    def test_complete_task_stores_result_and_is_idempotent(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        out = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="all good", diff_stat="1 file changed", duration_s=12.5)
        self.assertEqual(out["state"], "succeeded")
        self.assertTrue(out["stored"])
        again = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="all good", diff_stat="1 file changed", duration_s=12.5)
        self.assertFalse(again["stored"])  # 幂等
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["state"], "succeeded")
        self.assertEqual(got["result"]["log_summary"], "all good")
        # 错误 nonce 拒绝
        self.assertIsNone(task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce="wrong", exit_code=1,
            log_summary="", diff_stat="", duration_s=0))

    def test_failed_exit_code_marks_failed(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        out = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=3,
            log_summary="boom", diff_stat="", duration_s=1.0)
        self.assertEqual(out["state"], "failed")

    def test_expire_tasks_marks_old_queued_expired(self):
        task, _ = self._create(now=time.time() - 90000)
        expired = task_store.expire_tasks(now=time.time())
        self.assertEqual(expired, [task["task_id"]])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "expired")

    def test_expired_lease_does_not_requeue_newer_attempt(self):
        # 回归：A lease 过期→重派为 B；再过期 B 的旧 A lease 不能把 B 打回 queued
        task, _ = self._create()
        lease_a = task_store.lease_task(machine="mac-local", runner_id="rA", now=1000.0)
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "leased")
        # A 的 lease 过期，任务回 queued
        requeued = task_store.expire_leases(now=1000.0 + 301)
        self.assertEqual(requeued, [task["task_id"]])
        # B 重新领取（新 attempt_id）
        lease_b = task_store.lease_task(machine="mac-local", runner_id="rB", now=1301.0)
        self.assertNotEqual(lease_b["attempt_id"], lease_a["attempt_id"])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "leased")
        # 再次 expire_leases：B 的 lease 未过期，不应把 B 打回 queued；
        # A 的过期 lease 行 attempt 已不是任务最新 attempt，同样被 fence。
        requeued2 = task_store.expire_leases(now=1301.0 + 100)
        self.assertEqual(requeued2, [])
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "leased")

    def test_stale_attempt_completion_rejected(self):
        # 回归：A lease 过期后任务被 B 重新领取，A 的过期完成必须被拒绝，且不能改 B
        task, _ = self._create()
        lease_a = task_store.lease_task(machine="mac-local", runner_id="rA", now=1000.0)
        self.assertEqual(task_store.expire_leases(now=1000.0 + 301), [task["task_id"]])
        lease_b = task_store.lease_task(machine="mac-local", runner_id="rB", now=1301.0)
        # B 心跳 → running
        heartbeat_b = task_store.heartbeat(attempt_id=lease_b["attempt_id"],
                                           nonce=lease_b["nonce"], now=1302.0)
        self.assertEqual(heartbeat_b["task_id"], task["task_id"])
        # A 尝试用旧 attempt 提交结果 → 拒绝（task.attempt_id 已是 B）
        stale = task_store.complete_task(
            attempt_id=lease_a["attempt_id"], nonce=lease_a["nonce"], exit_code=0,
            log_summary="stale", diff_stat="", duration_s=1.0, now=1310.0)
        self.assertIsNone(stale)
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["state"], "running")
        self.assertIsNone(got["result"])  # A 的结果不得写入
        # B 仍可正常完成
        done = task_store.complete_task(
            attempt_id=lease_b["attempt_id"], nonce=lease_b["nonce"], exit_code=0,
            log_summary="ok", diff_stat="", duration_s=1.0, now=1320.0)
        self.assertEqual(done["state"], "succeeded")
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["result"]["log_summary"], "ok")

    def test_duplicate_completion_returns_persisted_state(self):
        # 回归：同一 attempt 重复提交时返回已落库状态，不按新 exit_code 重算
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        first = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="good", diff_stat="", duration_s=1.0)
        self.assertEqual(first["state"], "succeeded")
        # 第二次用相反 exit_code（失败）提交 → 返回已持久化的 succeeded，且不覆盖
        dup = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=1,
            log_summary="good", diff_stat="", duration_s=1.0)
        self.assertFalse(dup["stored"])
        self.assertEqual(dup["state"], "succeeded")
        got = task_store.get_task(task["task_id"])
        self.assertEqual(got["state"], "succeeded")
        self.assertEqual(got["result"]["exit_code"], 0)

    def test_complete_task_truncates_log_and_diff(self):
        # 回归：log_summary>10240、diff_stat>5120 必须服务端截断，不得触发 CHECK
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        out = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="L" * 20000, diff_stat="D" * 10000, duration_s=1.0)
        self.assertTrue(out["stored"])
        got = task_store.get_task(task["task_id"])
        self.assertEqual(len(got["result"]["log_summary"]), 10240)
        self.assertEqual(len(got["result"]["diff_stat"]), 5120)


class CancelRetryTests(TaskStoreTestBase):
    def test_cancel_queued_task(self):
        task, _ = self._create()
        row, changed = task_store.cancel_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "cancelled")
        # 已取消不可再领
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r1"))

    def test_cancel_terminal_is_noop(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"],
                                 exit_code=0, log_summary="", diff_stat="", duration_s=1)
        row, changed = task_store.cancel_task(task["task_id"], "op@example.com")
        self.assertFalse(changed)
        self.assertEqual(row["state"], "succeeded")

    def test_cancel_missing_returns_none(self):
        row, changed = task_store.cancel_task("t-missing", "op@example.com")
        self.assertIsNone(row)
        self.assertFalse(changed)

    def test_retry_failed_task_requeues_with_new_attempt(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"],
                                 exit_code=1, log_summary="", diff_stat="", duration_s=1)
        row, changed = task_store.retry_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "queued")
        lease2 = task_store.lease_task(machine="mac-local", runner_id="r2")
        self.assertEqual(lease2["task_id"], task["task_id"])

    def test_retry_active_task_rejected(self):
        task, _ = self._create()
        row, changed = task_store.retry_task(task["task_id"], "op@example.com")
        self.assertFalse(changed)
        self.assertEqual(row["state"], "queued")

    def test_heartbeat_after_cancel_returns_none_and_does_not_extend(self):
        # 回归：取消后旧 attempt 的心跳必须失败，且不得顺延 lease（否则活着的 lease
        # 与 cancelled 状态矛盾，runner API 会误判租约仍有效）
        t0 = 1000.0
        task, _ = self._create(now=t0)
        lease = task_store.lease_task(machine="mac-local", runner_id="r1", now=t0)
        # 正常心跳成功（leased→running，续租到 t0+0.5+LEASE_TTL_S）
        ok = task_store.heartbeat(attempt_id=lease["attempt_id"], nonce=lease["nonce"],
                                  now=t0 + 0.5)
        self.assertIsNotNone(ok)
        self.assertEqual(task_store.get_task(task["task_id"])["state"], "running")
        last_expiry = task_store._iso(t0 + 0.5 + task_store.LEASE_TTL_S)
        # 取消
        row, changed = task_store.cancel_task(task["task_id"], "op@example.com", now=t0 + 60.0)
        self.assertTrue(changed)
        self.assertEqual(row["state"], "cancelled")
        # 取消后心跳失败
        self.assertIsNone(task_store.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], now=t0 + 120.0))
        # lease 行未被顺延：expires_at 仍为最后一次成功心跳的值
        conn = task_store._connect()
        try:
            lease_row = conn.execute(
                "SELECT expires_at FROM leases WHERE attempt_id=?",
                (lease["attempt_id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(lease_row)
        self.assertEqual(lease_row["expires_at"], last_expiry)


class V4InitialGoalStoreTests(TaskStoreTestBase):
    """2026-09-09: patch / test_summary / pause-continue / confirm gate."""

    def test_complete_task_stores_diff_patch_and_test_summary(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        summary = {
            "framework": "pytest",
            "passed": 2,
            "failed": 1,
            "skipped": 0,
            "errors": 0,
            "duration_s": 1.5,
            "failed_names": ["test_foo"],
            "secret_key": "drop-me",
        }
        out = task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=1,
            log_summary="boom", diff_stat="1 file",
            diff_patch="diff --git a/x b/x\n+hello", test_summary=summary,
            duration_s=3.0)
        self.assertEqual(out["state"], "failed")
        got = task_store.get_task(task["task_id"])["result"]
        self.assertIn("+hello", got["diff_patch"])
        self.assertEqual(got["test_summary"]["failed"], 1)
        self.assertEqual(got["test_summary"]["failed_names"], ["test_foo"])
        self.assertNotIn("secret_key", got["test_summary"])

    def test_legacy_complete_leaves_new_result_columns_null(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="ok", diff_stat="", duration_s=1.0)
        got = task_store.get_task(task["task_id"])["result"]
        self.assertTrue(got["diff_patch"] in (None, ""))
        self.assertTrue(got["test_summary"] in (None, {}, ""))

    def test_pause_queued_then_continue_requeues_new_attempt(self):
        task, _ = self._create()
        old_attempt = task["attempt_id"]
        row, changed = task_store.pause_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "paused")
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r1"))
        row2, changed2 = task_store.continue_task(task["task_id"], "op@example.com")
        self.assertTrue(changed2)
        self.assertEqual(row2["state"], "queued")
        self.assertNotEqual(row2["attempt_id"], old_attempt)
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        self.assertEqual(lease["task_id"], task["task_id"])
        self.assertNotEqual(lease["attempt_id"], old_attempt)

    def test_pause_terminal_is_rejected(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="ok", diff_stat="", duration_s=1.0)
        row, changed = task_store.pause_task(task["task_id"], "op@example.com")
        self.assertFalse(changed)
        self.assertEqual(row["state"], "succeeded")

    def test_pause_running_rejects_heartbeat(self):
        task, _ = self._create()
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        task_store.heartbeat(attempt_id=lease["attempt_id"], nonce=lease["nonce"])
        row, changed = task_store.pause_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "paused")
        self.assertIsNone(task_store.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"]))
        self.assertIsNone(task_store.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
            log_summary="late", diff_stat="", duration_s=1.0))

    def test_confirm_gate_hides_task_from_lease_until_confirmed(self):
        task, _ = self._create(confirm=True)
        self.assertEqual(task["state"], "queued")
        gate = task_store.get_task_gate(task["task_id"])
        self.assertEqual(gate["state"], "pending")
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r1"))
        row, changed = task_store.confirm_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(task_store.get_task_gate(task["task_id"])["state"], "confirmed")
        lease = task_store.lease_task(machine="mac-local", runner_id="r1")
        self.assertEqual(lease["task_id"], task["task_id"])

    def test_reject_gate_cancels_task(self):
        task, _ = self._create(confirm=True)
        row, changed = task_store.reject_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(task_store.get_task_gate(task["task_id"])["state"], "rejected")
        self.assertIsNone(task_store.lease_task(machine="mac-local", runner_id="r1"))

    def test_init_migrates_existing_results_table(self):
        conn = task_store._connect()
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(results)").fetchall()}
        finally:
            conn.close()
        self.assertIn("diff_patch", cols)
        self.assertIn("test_summary", cols)
        conn = task_store._connect()
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        finally:
            conn.close()
        self.assertIn("task_gates", tables)

    def test_paused_can_be_cancelled(self):
        task, _ = self._create()
        task_store.pause_task(task["task_id"], "op@example.com")
        row, changed = task_store.cancel_task(task["task_id"], "op@example.com")
        self.assertTrue(changed)
        self.assertEqual(row["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()