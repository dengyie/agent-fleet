"""评审修复 #18：SupervisorService 只有仓储一条路径（无内存队列分支）；
未启用 supervisor 的 hub 不加载签名 key、不打开持久命令库。"""
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization as _ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hub.application.supervisor_service import SupervisorService, SupervisorServiceError
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository


def _priv():
    return Ed25519PrivateKey.generate().private_bytes(
        _ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())


class SingleRepositoryPathTests(unittest.TestCase):
    def test_no_in_memory_queue_state(self):
        sv = SupervisorService(signing_key=_priv())
        for attr in ("_commands", "_nonces", "_target_cmds", "_audit"):
            self.assertFalse(hasattr(sv, attr), attr)
        cmd = sv.enqueue("m1", "sess_1", None, "terminate_session", "x")
        self.assertIsInstance(sv._command_repository, SupervisorCommandRepository)
        self.assertIsNotNone(sv._command_repository.find_command(cmd["command_id"]))
        self.assertEqual([c["command_id"] for c in sv.poll("m1")], [cmd["command_id"]])
        self.assertTrue(any(row.get("event") == "enqueue" for row in sv.audit_recent()))

    def test_queue_survives_service_restart_on_same_repository(self):
        db = Path(tempfile.mkdtemp()) / "commands.db"
        key = _priv()
        first = SupervisorService(signing_key=key,
                                  command_repository=SupervisorCommandRepository(db))
        cmd = first.enqueue("m1", "sess_1", None, "terminate_session", "x")
        second = SupervisorService(signing_key=key,
                                   command_repository=SupervisorCommandRepository(db))
        self.assertEqual([c["command_id"] for c in second.poll("m1")], [cmd["command_id"]])
        receipt = second.receipt(cmd["command_id"], "succeeded", machine="m1")
        self.assertTrue(receipt["first"])
        replay = second.receipt(cmd["command_id"], "failed", machine="m1")
        self.assertEqual((replay["status"], replay["first"]), ("succeeded", False))

    def test_unwired_service_does_not_create_store_until_used(self):
        sv = SupervisorService(signing_key=None)
        self.assertIsNone(sv._repository)
        with self.assertRaises(SupervisorServiceError):
            sv.enqueue("m1", "sess_1", None, "terminate_session", "x")
        self.assertIsNone(sv._repository)


class BootstrapGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def app(self, enabled):
        cfg = FleetConfig.from_root(
            self.tmp, ingest_token="t", dev_operator="op@example.com",
            supervisor_enabled=enabled, supervisor_signing_raw=_priv())
        return create_app(cfg)

    def service(self, app):
        return app.extensions["fleet"]["services"]["supervisor"]

    def test_disabled_supervisor_has_no_key_and_no_store(self):
        sv = self.service(self.app(False))
        with self.assertRaises(SupervisorServiceError) as cm:
            sv.enqueue("m1", "sess_1", None, "terminate_session", "x")
        self.assertEqual(cm.exception.code, "supervisor_disabled")
        self.assertIsNone(sv._repository)
        self.assertFalse((self.tmp / "var" / "supervisor" / "commands.db").exists())

    def test_enabled_supervisor_uses_durable_store(self):
        sv = self.service(self.app(True))
        sv.enqueue("m1", "sess_1", None, "terminate_session", "x")
        self.assertTrue((self.tmp / "var" / "supervisor" / "commands.db").exists())


if __name__ == "__main__":
    unittest.main()
