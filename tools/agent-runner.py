#!/usr/bin/env python3
"""tools/agent-runner.py — agent 侧任务执行器（push/pull-only）

主循环：poll → lease → worktree → adapter → heartbeat → result。
runner 主动出站 HTTPS；hub 不反向连接。崩溃安全：lease 过期由 hub 重派。

用法:
  python3 tools/agent-runner.py [--config ~/.config/agent-fleet/runner.yaml]
                                [--once] [--interval 15]
"""
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _bootstrap_direct_imports():
    """LaunchAgent / 裸脚本直跑：把 repo root 挂到 sys.path[0]。

    plist 无 WorkingDirectory、repo root 不在 sys.path。repo root 上的
    ``agent_profiles``（tools.runner_config 依赖）与 ``report_schema``
    （tools.supervisor.supervisor 顶层 import）若解析不到，后者发生在
    任务认领 *之后* —— hub 侧表现为 leased 永无 result、lease 过期重派、
    循环认领（2026-09-16 卡点 B root cause）。root 置于 sys.path[0] 后
    （常规包优先于 sys.path 后位），上述 import 与 ``from tools import ...``
    全部可达，无需逐模块手工注册。
    """
    if __package__ not in (None, ""):
        return
    root = Path(__file__).resolve().parent.parent
    # Direct scripts put tools/ first, where platform/ shadows the stdlib.
    # Keep only the package root on sys.path, as Python -m already does.
    sys.path[:] = [entry for entry in sys.path if Path(entry).resolve() != root / 'tools']
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_direct_imports()

from tools import adapters, result_files, runner_config, worktree
from tools.adapters.base import EXIT_UNSAFE_INSTRUCTION
from tools.spawn_policy import UnsafeInstruction, refuse_windows_batch
from tools.transport import Transport

USER_AGENT = "agent-fleet-runner/1.0"
MAX_BACKOFF_S = 60
MIN_BACKOFF_S = 5
# heartbeat 日志上传约束：≤MAX_LOG_LINES 行 × ≤MAX_LOG_LINE_CHARS 字符（与 hub 一致）
MAX_LOG_LINES = 50
MAX_LOG_LINE_CHARS = 500
#: 本地租约期限：hub lease TTL 为 300s，超过此时长未成功续约即视为租约丢失并中止，
#: 避免 hub 已重派后本机仍在同一 worktree 上继续执行（双跑）。
LOCAL_LEASE_DEADLINE_S = 240
#: attempt_id 会拼进 URL 路径与 pending 文件名，必须是受限字符集
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: 结果提交时可重试的 4xx（其余 4xx 为终态拒绝，转入 pending/rejected/ 隔离）
_RETRYABLE_4XX = (408, 429)

#: 诊断日志：常驻模式写 cache_dir/runner.log（RotatingFileHandler 512KB×2），
#: --once 模式写 stderr。文件 IO 一律 UTF-8（Windows GBK locale 教训）。
log = logging.getLogger("agent-fleet.runner")
log.addHandler(logging.NullHandler())


def _setup_logging(cfg, resident: bool) -> None:
    """装配诊断日志输出目标（main 在 load_config 之后调用一次）。"""
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    log.setLevel(logging.INFO)
    if resident:
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(cfg.cache_dir / "runner.log",
                                      maxBytes=512 * 1024, backupCount=2,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(handler)


class RunnerPollError(RuntimeError):
    """轮询请求传输层失败（post_json 返回 status 0）——本轮拿不到任务，应退避。"""


def _bounded_log_lines(lines, max_lines=MAX_LOG_LINES, max_chars=MAX_LOG_LINE_CHARS):
    """发送防线：最多 max_lines 行、每行最多 max_chars 字符。

    生产者侧 _LogBuffer 已用 deque(maxlen) + 截断保证，此处双保险，
    防御未来调用方/线程抖动直接把超限 payload 送到 hub（50 行 × 500 字符约束）。
    """
    out = []
    for line in lines[-max_lines:]:
        line = str(line)
        if len(line) > max_chars:
            line = line[:max_chars]
        out.append(line)
    return out


class _LogBuffer:
    """有界日志缓冲：入队每行截断到 MAX_LOG_LINE_CHARS 字符，仅保留最新 MAX_LOG_LINES 行。

    供 heartbeat 每次上传抽样；旧行直接丢弃，保证任何时刻缓冲 ≤50 行 × 500 字符。
    thread-safe：适配器 stdout 线程 enqueue，heartbeat 线程 drain。
    """

    def __init__(self, max_lines=MAX_LOG_LINES, max_chars=MAX_LOG_LINE_CHARS):
        self._max_chars = max_chars
        self._lines = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def enqueue(self, line):
        line = str(line)
        if len(line) > self._max_chars:
            line = line[:self._max_chars]
        with self._lock:
            self._lines.append(line)

    def drain(self):
        with self._lock:
            lines = list(self._lines)
            self._lines.clear()
        return lines


def post_json(cfg, path, body, timeout=15):
    """POST JSON 到 hub；网络异常返回 (0, {...}) 不抛。

    出站策略由 cfg.transport（tools/transport.Transport，auto 模式）承载：
    直连被 CF 边缘拦截（403 error code: 101x）或传输失败时回退系统代理。
    body 只在此处经 transport 序列化一次。
    """
    if cfg.transport is None:
        cfg.transport = Transport()
    headers = {
        "Content-Type": "application/json",
        "X-Runner-Credential": f"{cfg.machine}:{cfg.credential}",
        "User-Agent": USER_AGENT,
    }
    return cfg.transport.post_json(cfg.hub + path, body, headers, timeout=timeout)


def _valid_attempt_id(value):
    return isinstance(value, str) and bool(_ATTEMPT_ID_RE.fullmatch(value)) \
        and value not in (".", "..")


class _LeaseClock:
    """本地租约时钟：记录最近一次成功续约，超过 LOCAL_LEASE_DEADLINE_S 即过期。

    heartbeat 网络长时间失败时 hub 侧 lease 会在 300s 后过期并重派；本地必须
    先一步停手，而不是无限期继续跑。
    """

    def __init__(self, deadline_s=None, clock=time.monotonic):
        self._deadline_s = float(LOCAL_LEASE_DEADLINE_S if deadline_s is None else deadline_s)
        self._clock = clock
        self._renewed = clock()

    def renewed(self):
        self._renewed = self._clock()

    def expired(self):
        return self._clock() - self._renewed > self._deadline_s


def _pending_dir(cfg):
    d = cfg.cache_dir / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_pending(cfg, attempt_id, body):
    """原子写入 pending 结果（tmp + os.replace），崩溃不会留下半写文件。"""
    if not _valid_attempt_id(attempt_id):
        log.error("attempt_id 非法，结果不缓存: %r", str(attempt_id)[:80])
        return
    target = _pending_dir(cfg) / f"{attempt_id}.json"
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log.error("pending 结果写入失败 %s: %s", target.name, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def _quarantine_pending(cfg, f, status):
    """终态 4xx：hub 永远不会接受，移入 pending/rejected/ 保留现场，不再重传。"""
    rejected = _pending_dir(cfg) / "rejected"
    try:
        rejected.mkdir(parents=True, exist_ok=True)
        os.replace(f, rejected / f.name)
        log.error("pending 结果被 hub 拒绝(status=%s)，已隔离到 rejected/%s",
                  status, f.name)
    except OSError as exc:
        log.error("pending 结果隔离失败 %s: %s", f.name, exc)


def flush_pending(cfg):
    """重传上次失败的结果（attempt_id 幂等，重复提交安全）。

    损坏的 pending 文件（半写/截断）无法重放，记录后丢弃，避免每轮 poll
    无声重试；重传失败保留文件并打到 stderr，保证结果送达可观测。
    """
    for f in sorted(_pending_dir(cfg).glob("*.json")):
        try:
            body = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("pending 结果损坏，丢弃 %s: %s", f.name, exc)
            try:
                f.unlink()
            except OSError as exc2:
                log.error("pending 文件删除失败 %s: %s", f.name, exc2)
            continue
        try:
            attempt_id = body.get("attempt_id") if isinstance(body, dict) else None
            if not _valid_attempt_id(attempt_id) or f.stem != attempt_id:
                _quarantine_pending(cfg, f, "invalid_attempt_id")
                continue
            status, _ = post_json(cfg, f"/api/commands/{attempt_id}/result", body)
            if status == 200:
                f.unlink()
            elif status == 409:
                # 409 = lease 已失效但结果已记录/任务已重派；删除前留痕
                log.warning("pending 结果被 hub 以 409 拒收（lease 已失效/已重派），丢弃 %s",
                            f.name)
                f.unlink()
            elif 400 <= status < 500 and status not in _RETRYABLE_4XX:
                _quarantine_pending(cfg, f, status)
        except Exception as exc:
            log.warning("pending 重传失败 %s: %s: %s",
                        f.name, type(exc).__name__, exc)


def _submit_result(cfg, attempt_id, nonce, result, diff_stat, duration_s,
                   files=None, diff_patch=None, test_summary=None):
    body = {
        "attempt_id": attempt_id, "nonce": nonce, "exit_code": result.exit_code,
        "log_summary": result.log_tail, "diff_stat": diff_stat,
        "duration_s": round(duration_s, 1),
    }
    if files:
        body["files"] = files
    if diff_patch:
        body["diff_patch"] = diff_patch
    if test_summary:
        body["test_summary"] = test_summary
    if not _valid_attempt_id(attempt_id):
        log.error("attempt_id 非法，拒绝提交结果: %r", str(attempt_id)[:80])
        return 0
    status, _ = post_json(cfg, f"/api/commands/{attempt_id}/result", body)
    if status != 200:
        _write_pending(cfg, attempt_id, body)
    return status


def _collect_outputs(wt):
    """收尾采集：diff/文件快照/patch/测试摘要逐项独立兜底。

    任一步失败只丢该项（记日志），不能把已完成任务的真实 exit_code 覆盖成 125。
    """
    out = {"diff": "", "files": [], "patch": "", "summary": None}
    if wt is None:
        return out
    steps = (
        ("diff", lambda: worktree.diff_stat(wt)),
        ("files", lambda: result_files.collect_result_files(wt)),
        ("patch", lambda: result_files.redact_patch(worktree.diff_patch(wt))[0]),
        ("summary", lambda: result_files.collect_test_summary(wt)),
    )
    for key, step in steps:
        try:
            out[key] = step()
        except Exception as exc:
            log.warning("结果采集步骤 %s 失败: %s: %s", key, type(exc).__name__, exc)
    return out


def _finish_capture(capture, exit_code):
    try:
        capture.finish(exit_code)
    except Exception as exc:
        log.warning("会话捕获收尾失败: %s: %s", type(exc).__name__, exc)


def _bf_supervisor():
    """Lazily expose tools.supervisor (only when managed mode is opted in)."""
    from tools.supervisor import supervisor as _s
    return _s


def _opaque(prefix):
    import secrets
    return f"{prefix}_{secrets.token_hex(8)}"


def run_task_managed(cfg, task, supervisor):
    """Run one leased task through the Agent Supervisor managed path.

    Spawns the allowlisted CLI via ``Supervisor.launch`` into its own process
    group, streams bounded lines through the same ``_LogBuffer``/heartbeat
    machinery, and submits the result through the identical ``_submit_result``
    contract as the legacy path.  Cancellation/abort is never a shell command:
    the supervisor only pauses / terminate the owned group.
    """
    project = cfg.projects.get(task["project"])
    if project is None:
        result = adapters.AdapterResult(
            exit_code=126, log_tail=f"项目 {task['project']} 不在 runner 白名单")
        _submit_result(cfg, task["attempt_id"], task["nonce"], result,
                       "", 0.0)
        return {"exit_code": 126}

    wt = None
    capture = None
    log_buffer = _LogBuffer()
    lease_lost = threading.Event()
    # bounded running tail for the final log_summary (mirrors adapter contract)
    tail_rows = []
    tail_bytes = [0]
    MAX_TAIL = 10240

    def on_line(line):
        log_buffer.enqueue(line)
        if capture is not None:
            capture.line(line)
        if len(line) > 500:
            line = line[:500]
        tail_rows.append(line)
        tail_bytes[0] += len(line) + 1
        while tail_bytes[0] > MAX_TAIL and tail_rows:
            tail_bytes[0] -= len(tail_rows.pop(0)) + 1

    lease = _LeaseClock()

    def heartbeat_loop():
        while not lease_lost.wait(cfg.heartbeat_interval_s):
            lines = _bounded_log_lines(log_buffer.drain())
            status, _ = post_json(cfg, f"/api/commands/{task['attempt_id']}/heartbeat",
                                  {"nonce": task["nonce"], "log_lines": lines})
            if status == 200:
                lease.renewed()
            elif status == 409:
                lease_lost.set()
                return

    def should_abort():
        return lease_lost.is_set() or lease.expired()

    started = time.monotonic()
    session_id = None
    hb = None
    try:
        # 先起 heartbeat：worktree 创建与 launch 期间同样需要续约
        hb = threading.Thread(target=heartbeat_loop,
                              name="lease-heartbeat", daemon=True)
        hb.start()
        wt = worktree.create_worktree(project, task["task_id"])
        agent_cfg = cfg.agents.get(task["agent_type"], {})
        adapter = adapters.create(task["agent_type"], command=agent_cfg.get("command"))
        timeout_s = agent_cfg.get("timeout_s", 1800)
        if not adapter.command:
            result = adapters.AdapterResult(
                exit_code=127, log_tail="managed: adapter 未配置 command")
            _submit_result(cfg, task["attempt_id"], task["nonce"], result,
                           "", time.monotonic() - started)
            return {"exit_code": 127}

        # 与 legacy 路径同一套 argv/env 策略：'-' 开头的 instruction 插 '--' 或拒绝，
        # Windows .cmd/.bat 不接受不可信参数，子进程只拿 allow-list 环境变量。
        env = adapter.child_env()
        try:
            argv = adapter.build_argv(str(task["instruction"]))
            refuse_windows_batch(argv, str(task["instruction"]), env)
        except UnsafeInstruction as exc:
            result = adapters.AdapterResult(
                exit_code=EXIT_UNSAFE_INSTRUCTION,
                log_tail=f"拒绝执行: {exc}")
            _submit_result(cfg, task["attempt_id"], task["nonce"], result,
                           "", time.monotonic() - started)
            return {"exit_code": result.exit_code}

        from tools.session.runner_capture import RunnerCapture, session_prefix
        session_id = session_prefix(cfg.machine) + task["attempt_id"]
        manifest = supervisor.launch(
            agent=task.get("agent_type", "unknown"),
            command=argv,
            cwd=str(wt),
            env_allowlist=env,
            session_id=session_id,
            attempt_id=task["attempt_id"],
        )
        capture = RunnerCapture(cfg, task, post_json, process_group_id=manifest.process_group_id)
        capture.start_upload()
        result = supervisor.wait_session(
            manifest.session_id,
            timeout_s=timeout_s,
            on_line=on_line,
            should_abort=should_abort,
        )
        # map ManagedRunResult onto AdapterResult shape for the identical
        # _submit_result contract
        adapter_result = adapters.AdapterResult(
            exit_code=result.exit_code,
            log_tail="\n".join(tail_rows),
            timed_out=result.timed_out,
            aborted=result.aborted,
        )
        lease_lost.set()
        hb.join(timeout=20)
        _finish_capture(capture, adapter_result.exit_code)
        duration = time.monotonic() - started
        out = _collect_outputs(wt)
        _submit_result(cfg, task["attempt_id"], task["nonce"],
                       adapter_result, out["diff"], duration, files=out["files"],
                       diff_patch=out["patch"] or None, test_summary=out["summary"])
        return {"exit_code": adapter_result.exit_code}
    except Exception as exc:
        lease_lost.set()
        result = adapters.AdapterResult(
            exit_code=125, log_tail=f"runner 内部错误: {type(exc).__name__}: {exc}"[:500])
        _submit_result(cfg, task["attempt_id"], task["nonce"],
                       result, "", time.monotonic() - started)
        return {"exit_code": 125}
    finally:
        lease_lost.set()
        if capture is not None:
            capture.stop_upload()
            capture.bridge.close()
        # 删除 worktree 前确保进程组已死：异常路径下组内进程可能仍在写 worktree
        _ensure_session_dead(supervisor, session_id)
        if wt is not None:
            worktree.cleanup_worktree(project, wt, task["task_id"])


def _ensure_session_dead(supervisor, session_id):
    if session_id is None:
        return
    try:
        state = supervisor.get(session_id).state
    except Exception:
        return  # 未成功 launch（或已被回收）——没有可终止的进程组
    if state in _bf_supervisor().TERMINAL_STATES:
        return
    try:
        supervisor.terminate_session(session_id, grace_s=5.0)
    except Exception as exc:
        log.error("收尾终止托管会话失败 %s: %s: %s",
                  session_id, type(exc).__name__, exc)


def run_task(cfg, task):
    """执行一个已领取的任务。返回 {"exit_code", ...} 摘要。

    task["project"] 必须命中 cfg.projects 绝对路径白名单，否则拒绝（exit_code 126）。
    每任务使用 worktree.create_worktree 隔离；heartbeat 线程在 adapter 执行期间续约，
    409 视为 lease 失效并中止 adapter；最终结果含 nonce 与有界 log/diff，提交失败缓存到 pending。
    """
    task_id = task["task_id"]
    attempt_id = task["attempt_id"]
    nonce = task["nonce"]
    started = time.monotonic()

    project = cfg.projects.get(task["project"])
    if project is None:
        result = adapters.AdapterResult(
            exit_code=126, log_tail=f"项目 {task['project']} 不在 runner 白名单")
        _submit_result(cfg, attempt_id, nonce, result, "", 0.0)
        return {"exit_code": 126}

    wt = None
    capture = None
    # 有界日志缓冲：入队截断 ≤500 字符/行，仅保留最新 50 行 ——
    # heartbeat 上传恒 ≤50 行 × 500 字符（与 hub 契约一致，旧行安全丢弃）。
    log_buffer = _LogBuffer()
    lease_lost = threading.Event()

    def on_line(line):
        log_buffer.enqueue(line)
        if capture is not None:
            capture.line(line)

    lease = _LeaseClock()

    def heartbeat_loop():
        while not lease_lost.wait(cfg.heartbeat_interval_s):
            lines = _bounded_log_lines(log_buffer.drain())
            status, _ = post_json(cfg, f"/api/commands/{attempt_id}/heartbeat",
                                  {"nonce": nonce, "log_lines": lines})
            if status == 200:
                lease.renewed()
            elif status == 409:
                lease_lost.set()  # adapter 将在 1s 内被 should_abort 终止
                return

    def should_abort():
        # 409 / 本地租约到期（长时间续约失败）中止 adapter；
        # 会话捕获失败只降级（RunnerCapture.degraded），不中止健康任务。
        return lease_lost.is_set() or lease.expired()

    try:
        from tools.session.runner_capture import RunnerCapture
        capture = RunnerCapture(cfg, task, post_json)
        capture.start_upload()
        wt = worktree.create_worktree(project, task_id)
        agent_cfg = cfg.agents.get(task["agent_type"], {})
        adapter = adapters.create(task["agent_type"],
                                  command=agent_cfg.get("command"),
                                  timeout_s=agent_cfg.get("timeout_s", 1800))
        hb = threading.Thread(target=heartbeat_loop, name="lease-heartbeat", daemon=True)
        hb.start()
        result = adapter.run(task["instruction"], wt, on_line=on_line,
                             should_abort=should_abort)
        lease_lost.set()  # 停 heartbeat 线程
        hb.join(timeout=20)
        _finish_capture(capture, result.exit_code)
        duration = time.monotonic() - started
        out = _collect_outputs(wt)
        _submit_result(cfg, attempt_id, nonce, result, out["diff"], duration,
                       files=out["files"], diff_patch=out["patch"] or None,
                       test_summary=out["summary"])
        return {"exit_code": result.exit_code}
    except Exception as exc:
        lease_lost.set()
        result = adapters.AdapterResult(
            exit_code=125, log_tail=f"runner 内部错误: {type(exc).__name__}: {exc}"[:500])
        _submit_result(cfg, attempt_id, nonce, result, "", time.monotonic() - started)
        return {"exit_code": 125}
    finally:
        lease_lost.set()
        if capture is not None:
            capture.stop_upload()
            capture.bridge.close()
        if wt is not None:
            worktree.cleanup_worktree(project, wt, task_id)


def poll_once(cfg):
    """单轮：先重传缓存结果，再 poll；有任务则执行。返回是否有任务。

    轮询请求网络失败（post_json 返回 status 0）抛 RunnerPollError，
    由 main() 转为指数退避——这是全局 5s→60s 退避约束的实现点。
    """
    from tools.session.runner_capture import replay_pending
    replay_pending(cfg, post_json)
    flush_pending(cfg)
    status, data = post_json(cfg, "/api/commands/poll", {"runner_id": cfg.runner_id})
    if status == 0:
        raise RunnerPollError(str(data.get("error", "网络不可达")))
    if status != 200 or not data.get("ok"):
        return False
    task = data.get("task")
    if not task:
        return False
    if not isinstance(task, dict) or not _valid_attempt_id(task.get("attempt_id")):
        # attempt_id 会进入 URL 与本地文件名；格式不对既无法安全执行也无法回报
        log.error("hub 下发的任务 attempt_id 非法，忽略: %r",
                  str(task.get("attempt_id") if isinstance(task, dict) else task)[:80])
        return False
    if cfg.managed_enabled:
        run_task_managed(cfg, task, _bf_supervisor().Supervisor(
            manifest_dir=cfg.supervisor_manifest_dir,
            machine_id=cfg.machine))
    else:
        run_task(cfg, task)
    return True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="agent-fleet runner（pull-only 任务执行器）")
    parser.add_argument("--config", default=str(runner_config.DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="单轮执行（cron 模式）")
    parser.add_argument("--interval", type=int, default=None, help="常驻轮询间隔秒")
    args = parser.parse_args(argv)

    cfg = runner_config.load_config(args.config)
    interval = max(1, args.interval or cfg.poll_interval_s)  # 间隔下限 1s，防止空转打爆 hub

    resident = not args.once
    _setup_logging(cfg, resident=resident)

    if args.once:
        try:
            poll_once(cfg)
        except RunnerPollError as exc:
            log.error("hub 不可达: %s", exc)
        return 0

    backoff = MIN_BACKOFF_S
    while True:
        try:
            did_work = poll_once(cfg)
            backoff = MIN_BACKOFF_S  # poll 成功（含空任务）→ 重置退避
            if not did_work:
                time.sleep(interval)
        except RunnerPollError as exc:
            # 网络失败：保留/递增 backoff，5s→60s 上限
            log.warning("hub 不可达，%ds 后重试: %s", backoff, exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("轮询异常")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


if __name__ == "__main__":
    raise SystemExit(main())