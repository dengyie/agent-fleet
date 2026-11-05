"""tools/adapters/base.py — adapter 执行核心

子进程执行 agent CLI；instruction 只作为单 argv 元素传递（不经 shell）。
输出行流式回调 on_line；should_abort 轮询用于 lease 失效后主动终止。
子进程环境走 spawn_policy 白名单（与 managed 路径共用），runner 凭据不继承。
"""
import subprocess
from dataclasses import dataclass

from tools.bounded_process import ProcessAborted, run_streamed
from tools.spawn_policy import (UnsafeInstruction, build_child_env,
                                place_instruction, refuse_windows_batch)

#: instruction 无法安全放入 argv（EX_USAGE）。
EXIT_UNSAFE_INSTRUCTION = 64

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
    # 默认 CLI 已知支持 "--" 结束选项解析；自定义 command 一律视为不支持。
    end_of_options = False
    # 该 CLI 自身需要的额外环境键（API key / 配置目录），叠加在通用白名单上。
    env_keys = ()

    def __init__(self, command=None, timeout_s=1800):
        self.command = command if command is not None else self.default_command
        self.timeout_s = int(timeout_s)

    def build_argv(self, instruction):
        """instruction 以 '-' 开头：支持 '--' 的默认 CLI 插 '--'，否则抛 UnsafeInstruction。"""
        supports_eoo = bool(self.end_of_options and self.default_command
                            and list(self.command) == list(self.default_command))
        return place_instruction(self.command, instruction, end_of_options=supports_eoo)

    def child_env(self):
        return build_child_env(self.env_keys)

    def run(self, instruction, workdir, on_line=None, should_abort=None):
        if not self.command:
            return AdapterResult(exit_code=127,
                                 log_tail=f"adapter {self.name} 未配置 command")
        env = self.child_env()
        try:
            argv = self.build_argv(instruction)
            refuse_windows_batch(argv, instruction, env)
        except UnsafeInstruction as exc:
            return AdapterResult(exit_code=EXIT_UNSAFE_INSTRUCTION,
                                 log_tail=f"拒绝执行: {exc}")
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
                on_line=feed, should_abort=should_abort, env=env)
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
