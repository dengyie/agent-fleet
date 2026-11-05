"""tools/adapters/base.py — adapter 执行核心

子进程执行 agent CLI；instruction 只作为单 argv 元素传递（不经 shell）。
输出行流式回调 on_line；should_abort 轮询用于 lease 失效后主动终止。
"""
import subprocess
from dataclasses import dataclass

from tools.bounded_process import ProcessAborted, run_streamed

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
            original = line
            line = line[:MAX_LINE]
            tail.append(line)
            tail_len[0] += len(line) + 1
            while tail_len[0] > MAX_LOG_TAIL and tail:
                tail_len[0] -= len(tail.pop(0)) + 1
            if on_line:
                try:
                    on_line(original)
                except Exception:
                    pass

        try:
            exit_code = run_streamed(
                argv, cwd=workdir, timeout_s=self.timeout_s,
                on_line=feed, should_abort=should_abort)
        except OSError as exc:
            return AdapterResult(exit_code=127,
                                 log_tail=f"启动失败: {type(exc).__name__}: {exc}"[:MAX_LINE])
        except ProcessAborted:
            return AdapterResult(exit_code=130, log_tail="\n".join(tail) + "\n[aborted: lease lost]",
                                 aborted=True)
        except subprocess.TimeoutExpired:
            return AdapterResult(exit_code=124,
                                 log_tail="\n".join(tail) + f"\n[timeout after {self.timeout_s}s]",
                                 timed_out=True)
        return AdapterResult(exit_code=exit_code, log_tail="\n".join(tail))
