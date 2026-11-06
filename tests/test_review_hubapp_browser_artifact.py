"""评审修复 #12：截图上传编排迁入 hub/application/browser_artifact_service.py，
以显式异常表达失败；HTTP 视图只做传输层读取。"""
import hashlib
import struct
import time
import unittest
import zlib

from hub.application.browser_artifact_service import (
    MAX_SCREENSHOT_BYTES,
    BrowserArtifactError,
    BrowserArtifactService,
    validate_screenshot_payload,
)
from hub.application.task_service import ApplicationError
from hub.infrastructure.browser_repository import BrowserRepositoryError
from hub.platform.artifacts import ArtifactError


def _png(width=2, height=1):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class _Commands:
    def __init__(self, row):
        self.row = row

    def get(self, command_id):
        return self.row if self.row and self.row["command_id"] == command_id else None


class _BrowserRepo:
    def __init__(self, *, state="issued", window_id=None, fail_complete=None):
        self.state = state
        self.window_id = window_id
        self.fail_complete = fail_complete
        self.calls = []

    def validate_artifact_upload_scope(self, ticket_id, owner_id, **kw):
        self.calls.append("validate")
        return {"state": self.state, "window_id": self.window_id,
                "expires_at": time.time() + 60}

    def begin_artifact_upload(self, ticket_id, token, **kw):
        self.calls.append("begin")
        ticket = {"ticket_id": ticket_id, "owner_id": "o1", "workspace_id": "w1",
                  "state": self.state}
        if self.state == "consumed":
            ticket["artifact_id"] = "a-existing"
        return ticket

    def complete_artifact_upload(self, ticket_id, **kw):
        self.calls.append("complete")
        if self.fail_complete:
            raise self.fail_complete
        self.completed = kw

    def reset_artifact_upload(self, ticket_id):
        self.calls.append("reset")


class _Store:
    def __init__(self):
        self.items = {("o1", "w1", "a-existing"): {"artifact_id": "a-existing"}}
        self.deleted = []

    def get(self, owner, workspace, artifact_id):
        return self.items.get((owner, workspace, artifact_id))

    def put_bytes(self, owner, workspace, raw, **kw):
        row = {"artifact_id": "a-new", "sha256": hashlib.sha256(raw).hexdigest(),
               "size": len(raw)}
        self.items[(owner, workspace, "a-new")] = row
        return row

    def delete(self, owner, workspace, artifact_id):
        self.deleted.append(artifact_id)
        self.items.pop((owner, workspace, artifact_id), None)


def _command(**over):
    row = {"command_id": "c1", "owner_id": "o1", "target_node": "n1",
           "action": "tool.browser.screenshot", "status": "leased",
           "lease_owner": "w1", "lease_until": time.time() + 60,
           "expires_at": time.time() + 60, "resource_id": "w1", "run_id": "r1",
           "arguments": {"session_id": "s1"}}
    row.update(over)
    return row


class BrowserArtifactServiceTests(unittest.TestCase):
    def service(self, repo, command=None, store=None):
        return BrowserArtifactService(
            browser_repository=repo, command_repository=_Commands(command or _command()),
            artifact_store=store or _Store())

    def upload(self, service, **over):
        kw = dict(owner_id="o1", node_id="n1", token="t", command_id="c1",
                  worker_id="w1", idempotency_key="k", raw=_png())
        kw.update(over)
        return service.upload_screenshot("tk1", **kw)

    def test_payload_validation_errors_are_explicit(self):
        with self.assertRaises(BrowserArtifactError) as cm:
            validate_screenshot_payload("image/jpeg", _png())
        self.assertEqual((cm.exception.code, cm.exception.status), ("invalid_content_type", 415))
        with self.assertRaises(BrowserArtifactError) as cm:
            validate_screenshot_payload("image/png", _png() + b"\x00" * MAX_SCREENSHOT_BYTES)
        self.assertEqual((cm.exception.code, cm.exception.status), ("artifact_too_large", 413))
        self.assertTrue(issubclass(BrowserArtifactError, ApplicationError))

    def test_successful_upload_completes_ticket(self):
        repo, store = _BrowserRepo(), _Store()
        artifact = self.upload(self.service(repo, store=store))
        self.assertEqual(artifact["artifact_id"], "a-new")
        self.assertEqual(repo.calls, ["validate", "begin", "complete"])
        self.assertIsNone(repo.completed["frame_publisher"])

    def test_consumed_ticket_replays_existing_artifact_without_lease(self):
        repo = _BrowserRepo(state="consumed")
        # 租约已过期也能幂等重放已完成的上传
        artifact = self.upload(self.service(repo, _command(lease_until=0)))
        self.assertEqual(artifact["artifact_id"], "a-existing")
        self.assertNotIn("complete", repo.calls)

    def test_failure_resets_ticket_and_removes_orphan(self):
        repo, store = _BrowserRepo(fail_complete=BrowserRepositoryError("frame_scope")), _Store()
        with self.assertRaises(BrowserRepositoryError):
            self.upload(self.service(repo, store=store))
        self.assertEqual(repo.calls[-1], "reset")
        self.assertEqual(store.deleted, ["a-new"])

    def test_window_ticket_without_execution_windows_fails_frame_scope(self):
        repo, store = _BrowserRepo(window_id="win"), _Store()
        repo.complete_artifact_upload = lambda ticket_id, **kw: kw["frame_publisher"](
            None, {}, kw["frame_dimensions"])
        with self.assertRaises(BrowserRepositoryError) as cm:
            self.upload(self.service(repo, store=store))
        self.assertEqual(cm.exception.code, "frame_scope")
        self.assertEqual(store.deleted, ["a-new"])

    def test_command_scope_errors(self):
        cases = [
            (_command(owner_id="other"), "command_not_found", 404),
            (_command(target_node="n2"), "node_mismatch", 403),
            (_command(action="tool.browser.open"), "artifact_command_required", 400),
            (_command(lease_owner="w2"), "artifact_lease_mismatch", 409),
        ]
        for command, code, status in cases:
            with self.subTest(code=code):
                with self.assertRaises(BrowserArtifactError) as cm:
                    self.upload(self.service(_BrowserRepo(), command))
                self.assertEqual((cm.exception.code, cm.exception.status), (code, status))

    def test_overlong_worker_rejected(self):
        with self.assertRaises(BrowserArtifactError) as cm:
            self.upload(self.service(_BrowserRepo()), worker_id="w" * 129)
        self.assertEqual(cm.exception.code, "invalid_worker")

    def test_missing_artifact_on_replay_is_artifact_error(self):
        store = _Store()
        store.items.clear()
        with self.assertRaises(ArtifactError):
            self.upload(self.service(_BrowserRepo(state="consumed"), store=store))


if __name__ == "__main__":
    unittest.main()
