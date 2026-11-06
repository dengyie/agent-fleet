"""Review regressions: bounded draining, group reaping, output loss (findings 4, 5, 9)."""
import io
import os
import signal
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from fleet_core.process import bounded_process
from tools.supervisor import supervisor as sup_mod


def _kill_pid_file(path: Path) -> None:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _pid_gone(pid: int, wait_s: float = 3.0) -> bool:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@unittest.skipIf(os.name == "nt", "setsid is POSIX-only")
class DrainDeadlineTests(unittest.TestCase):
    def test_escaped_grandchild_holding_pipe_does_not_block_past_timeout(self):
        temp = Path(tempfile.mkdtemp())
        pid_file = temp / "grandchild.pid"
        script = (
            "import os, subprocess, sys\n"
            "code = 'import os,time,sys; os.setsid(); open(sys.argv[1],\"w\").write(str(os.getpid())); time.sleep(30)'\n"
            "subprocess.Popen([sys.executable, '-c', code, sys.argv[1]])\n"
            "print('parent-done', flush=True)\n"
        )
        lines = []
        started = time.monotonic()
        try:
            try:
                bounded_process.run_streamed(
                    [sys.executable, "-c", script, str(pid_file)], cwd=temp,
                    timeout_s=2.0, on_line=lines.append)
            except Exception:
                pass
            elapsed = time.monotonic() - started
        finally:
            for _ in range(40):
                if pid_file.exists():
                    break
                time.sleep(0.05)
            _kill_pid_file(pid_file)
        self.assertLess(elapsed, 4.0, f"run_streamed blocked {elapsed:.1f}s")
        self.assertIn("parent-done", lines)

    def test_overlong_line_is_marked_not_silently_dropped(self):
        temp = Path(tempfile.mkdtemp())
        size = (1 << 20) + 4096
        lines = []
        bounded_process.run_streamed(
            [sys.executable, "-c", f"import sys; sys.stdout.write('x'*{size}+'\\nend\\n')"],
            cwd=temp, timeout_s=20, on_line=lines.append)
        self.assertEqual(lines[-1], "end")
        self.assertIn("truncated", lines[0][-80:])
        self.assertLessEqual(len(lines[0]), (1 << 20) + 64)


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class GroupReapTests(unittest.TestCase):
    def test_term_trapping_grandchild_is_killed_after_timeout(self):
        temp = Path(tempfile.mkdtemp())
        pid_file = temp / "gc.pid"
        grandchild = (
            "import signal,time,os,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)"
        )
        script = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]])\n"
            "time.sleep(60)\n"
        )
        sup = sup_mod.Supervisor(manifest_dir=temp / "sup", machine_id="m1")
        try:
            manifest = sup.launch(agent="codex", command=[sys.executable, "-c", script,
                                                           str(pid_file)],
                                  cwd=str(temp), env_allowlist={})
            for _ in range(100):
                if pid_file.exists() and pid_file.read_text().strip():
                    break
                time.sleep(0.05)
            gpid = int(pid_file.read_text().strip())
            result = sup.wait_session(manifest.session_id, timeout_s=1.0)
            self.assertTrue(result.timed_out)
            self.assertTrue(_pid_gone(gpid), "TERM-trapping grandchild survived")
        finally:
            _kill_pid_file(pid_file)


class SupervisorTruncationTests(unittest.TestCase):
    def test_overlong_managed_line_gets_marker(self):
        temp = Path(tempfile.mkdtemp())
        sup = sup_mod.Supervisor(manifest_dir=temp / "sup", machine_id="m1")
        size = sup_mod._ABUF_MAX + 10000
        manifest = sup.launch(
            agent="codex",
            command=[sys.executable, "-c",
                     f"import sys; sys.stdout.write('y'*{size}+'\\nafter\\n'); sys.stdout.flush()"],
            cwd=str(temp), env_allowlist={})
        lines = []
        result = sup.wait_session(manifest.session_id, timeout_s=30, on_line=lines.append)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("after", lines)
        marked = [line for line in lines if "truncated" in line]
        self.assertEqual(len(marked), 1, [line[-60:] for line in lines])


class WindowsReaderBackpressureTests(unittest.TestCase):
    def test_full_queue_blocks_instead_of_dropping(self):
        chunks = 300
        payload = b"".join(b"%05d\n" % i for i in range(chunks))
        handle = types.SimpleNamespace(
            proc=types.SimpleNamespace(stdout=io.BytesIO(payload)))
        ops = types.SimpleNamespace(_READ_CHUNK=6)
        sup_mod.WindowsGroupOps._ensure_reader(ops, handle)
        time.sleep(1.0)  # 队列（256）在无人消费时被写满
        received = []
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                received.append(handle._queue.get(timeout=0.2))
            except Exception:
                if getattr(handle, "_reader_eof", False) and handle._queue.empty():
                    break
        self.assertEqual(len(received), chunks)
        handle._reader_closed = True


if __name__ == "__main__":
    unittest.main()
