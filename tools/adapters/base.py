"""tools/adapters/base.py — adapter 执行核心

子进程执行 agent CLI；instruction 只作为单 argv 元素传递（不经 shell）。
输出行流式回调 on_line；should_abort 轮询用于 lease 失效后主动终止。
"""
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_LOG_TAIL = 10240
MAX_LINE = 500


@dataclass
class AdapterResult:
    exit_code: int
    log_tail: str
    timed_out: bool = False
    aborted: bool = False


class BaseAdapter:
    name = "base"
    default_command = None  # list[str]，"{instruction}" 为插值点

    def __init__(self, command=None, timeout_s=1800):
        self.command = command if command is not None else self.default_command
        self.timeout_s = int(timeout_s)

    def build_argv(self, instruction):
        argv = list(self.command)
        if "{instruction}" in argv:
            return [instruction if c == "{instruction}" else c for c in argv]
        return argv + [instruction]

    def run(self, instruction, workdir, on_line=None, should_abort=None):
        if not self.command:
            return AdapterResult(exit_code=127,
                                 log_tail=f"adapter {self.name} 未配置 command")
        argv = self.build_argv(instruction)
        tail = []
        tail_len = [0]

        def feed(line):
            line = line[:MAX_LINE]
            tail.append(line)
            tail_len[0] += len(line) + 1
            while tail_len[0] > MAX_LOG_TAIL and tail:
                tail_len[0] -= len(tail.pop(0)) + 1
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
                start_new_session=True,  # 独立进程组，超时/中止可整组杀
            )
        except OSError as exc:
            return AdapterResult(exit_code=127,
                                 log_tail=f"启动失败: {type(exc).__name__}: {exc}"[:MAX_LINE])

        timed_out = threading.Event()

        def watchdog():
            proc.wait()
            timed_out.set()

        waiter = threading.Thread(target=watchdog, daemon=True)
        waiter.start()

        def reader():
            try:
                for line in proc.stdout:
                    feed(line.rstrip("\n"))
            except Exception:
                pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        deadline = time.monotonic() + self.timeout_s
        aborted = False
        while not timed_out.wait(1.0):
            if should_abort and should_abort():
                aborted = True
                _kill(proc)
                break
            if time.monotonic() > deadline:
                _kill(proc)
                break
        timed_out.wait(10)  # 等进程确认退出
        reader_thread.join(timeout=5)
        try:
            proc.stdout.close()
        except Exception:
            pass

        if aborted:
            return AdapterResult(exit_code=130, log_tail="\n".join(tail) + "\n[aborted: lease lost]",
                                 aborted=True)
        if proc.returncode is None or (proc.returncode and proc.returncode < 0 and
                                       time.monotonic() > deadline):
            return AdapterResult(exit_code=124,
                                 log_tail="\n".join(tail) + f"\n[timeout after {self.timeout_s}s]",
                                 timed_out=True)
        return AdapterResult(exit_code=proc.returncode or 0, log_tail="\n".join(tail))


def _kill(proc):
    import os
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass