#!/usr/bin/env python3
"""tools/agent-runner.py — agent 侧任务执行器（push/pull-only）

主循环：poll → lease → worktree → adapter → heartbeat → result。
runner 主动出站 HTTPS；hub 不反向连接。崩溃安全：lease 过期由 hub 重派。

用法:
  python3 tools/agent-runner.py [--config ~/.config/agent-fleet/runner.yaml]
                                [--once] [--interval 15]
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path


def _bootstrap_direct_imports():
    """Expose the repository namespace package for direct script execution."""
    if __package__ not in (None, ""):
        return
    import importlib.util
    import types

    root = Path(__file__).resolve().parent.parent
    # ``tools.runner_config`` imports the repository-root ``agent_profiles``
    # module by name. Without registration a direct run resolves it against
    # the ambient interpreter paths and fails with ModuleNotFoundError —
    # same class of bug as the release-root ``tools`` anchor (commit a737839)
    # and the probe shim (tools/agent-self-report.py).
    if "agent_profiles" not in sys.modules:
        profiles_path = root / "agent_profiles.py"
        spec = importlib.util.spec_from_file_location(
            "agent_profiles", profiles_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_profiles"] = module
        spec.loader.exec_module(module)
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(root / "tools")]
    sys.modules.setdefault("tools", tools_package)


_bootstrap_direct_imports()

from tools import adapters, result_files, runner_config, worktree

USER_AGENT = "agent-fleet-runner/1.0"
MAX_BACKOFF_S = 60
MIN_BACKOFF_S = 5
#: Instant TLS/read timeouts (same class as ControlClient KeepAlive) retry
#: a few times before the outer 5s→60s backoff. HTTP 4xx/5xx are not retried.
_HTTP_ATTEMPTS = 3
_HTTP_RETRY_SLEEP_S = 0.4
_TRANSIENT_TRANSPORT = (TimeoutError, urllib.error.URLError, OSError,
                        ConnectionError, BrokenPipeError)
# heartbeat 日志上传约束：≤MAX_LOG_LINES 行 × ≤MAX_LOG_LINE_CHARS 字符（与 hub 一致）
MAX_LOG_LINES = 50
MAX_LOG_LINE_CHARS = 500


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
    """POST JSON 到 hub；网络异常返回 (0, {...}) 不抛。"""
    req = urllib.request.Request(
        cfg.hub + path,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Runner-Credential": f"{cfg.machine}:{cfg.credential}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    last_exc = None
    for attempt in range(_HTTP_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as extra:
            try:
                return extra.code, json.loads(extra.read().decode() or "{}")
            except Exception:
                return extra.code, {"error": "http_error"}
        except _TRANSIENT_TRANSPORT as extra:
            last_exc = extra
            if attempt + 1 < _HTTP_ATTEMPTS:
                time.sleep(_HTTP_RETRY_SLEEP_S)
            continue
        except Exception as extra:
            return 0, {"error": f"{type(extra).__name__}: {extra}"}
    return 0, {"error": f"{type(last_exc).__name__}: {last_exc}"}


def _pending_dir(cfg):
    d = cfg.cache_dir / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def flush_pending(cfg):
    """重传上次失败的结果（attempt_id 幂等，重复提交安全）。"""
    for f in sorted(_pending_dir(cfg).glob("*.json")):
        try:
            body = json.loads(f.read_text())
            status, _ = post_json(cfg, f"/api/commands/{body['attempt_id']}/result", body)
            if status in (200, 409):  # 409 = lease 已失效但结果已记录/任务已重派
                f.unlink()
        except Exception:
            pass


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
    status, _ = post_json(cfg, f"/api/commands/{attempt_id}/result", body)
    if status != 200:
        (_pending_dir(cfg) / f"{attempt_id}.json").write_text(
            json.dumps(body, ensure_ascii=False))
    return status


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
    log_buffer = _LogBuffer()
    lease_lost = threading.Event()
    # bounded running tail for the final log_summary (mirrors adapter contract)
    tail_rows = []
    tail_bytes = [0]
    MAX_TAIL = 10240

    def on_line(line):
        log_buffer.enqueue(line)
        if len(line) > 500:
            line = line[:500]
        tail_rows.append(line)
        tail_bytes[0] += len(line) + 1
        while tail_bytes[0] > MAX_TAIL and tail_rows:
            tail_bytes[0] -= len(tail_rows.pop(0)) + 1

    def heartbeat_loop():
        while not lease_lost.wait(cfg.heartbeat_interval_s):
            lines = _bounded_log_lines(log_buffer.drain())
            status, _ = post_json(cfg, f"/api/commands/{task['attempt_id']}/heartbeat",
                                  {"nonce": task["nonce"], "log_lines": lines})
            if status == 409:
                lease_lost.set()
                return

    started = time.monotonic()
    try:
        wt = worktree.create_worktree(project, task["task_id"])
        agent_cfg = cfg.agents.get(task["agent_type"], {})
        command = agent_cfg.get("command")
        timeout_s = agent_cfg.get("timeout_s", 1800)
        if not command:
            result = adapters.AdapterResult(
                exit_code=127, log_tail="managed: adapter 未配置 command")
            _submit_result(cfg, task["attempt_id"], task["nonce"], result,
                           "", time.monotonic() - started)
            return {"exit_code": 127}

        argv = list(command)
        if "{instruction}" in argv:
            argv = [str(task["instruction"]) if c == "{instruction}" else c
                    for c in argv]
        else:
            argv = argv + [str(task["instruction"])]

        manifest = supervisor.launch(
            agent=task.get("agent_type", "unknown"),
            command=argv,
            cwd=str(wt),
            env_allowlist={},
            session_id=_opaque("sess"),
            attempt_id=task["attempt_id"],
        )
        hb = threading.Thread(target=heartbeat_loop,
                              name="lease-heartbeat", daemon=True)
        hb.start()
        result = supervisor.wait_session(
            manifest.session_id,
            timeout_s=timeout_s,
            on_line=on_line,
            should_abort=lease_lost.is_set,
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
        diff = worktree.diff_stat(wt) if wt is not None else ""
        duration = time.monotonic() - started
        snapshot = result_files.collect_result_files(wt) if wt is not None else []
        patch = ""
        summary = None
        if wt is not None:
            patch, _ = result_files.redact_patch(worktree.diff_patch(wt))
            summary = result_files.collect_test_summary(wt)
        _submit_result(cfg, task["attempt_id"], task["nonce"],
                       adapter_result, diff, duration, files=snapshot,
                       diff_patch=patch or None, test_summary=summary)
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
        if wt is not None:
            worktree.cleanup_worktree(project, wt, task["task_id"])


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
    # 有界日志缓冲：入队截断 ≤500 字符/行，仅保留最新 50 行 ——
    # heartbeat 上传恒 ≤50 行 × 500 字符（与 hub 契约一致，旧行安全丢弃）。
    log_buffer = _LogBuffer()
    lease_lost = threading.Event()

    def on_line(line):
        log_buffer.enqueue(line)

    def heartbeat_loop():
        while not lease_lost.wait(cfg.heartbeat_interval_s):
            lines = _bounded_log_lines(log_buffer.drain())
            status, _ = post_json(cfg, f"/api/commands/{attempt_id}/heartbeat",
                                  {"nonce": nonce, "log_lines": lines})
            if status == 409:
                lease_lost.set()  # adapter 将在 1s 内被 should_abort 终止
                return

    try:
        wt = worktree.create_worktree(project, task_id)
        agent_cfg = cfg.agents.get(task["agent_type"], {})
        adapter = adapters.create(task["agent_type"],
                                  command=agent_cfg.get("command"),
                                  timeout_s=agent_cfg.get("timeout_s", 1800))
        hb = threading.Thread(target=heartbeat_loop, name="lease-heartbeat", daemon=True)
        hb.start()
        result = adapter.run(task["instruction"], wt, on_line=on_line,
                             should_abort=lease_lost.is_set)
        lease_lost.set()  # 停 heartbeat 线程
        diff = worktree.diff_stat(wt)
        duration = time.monotonic() - started
        snapshot = result_files.collect_result_files(wt)
        patch, _ = result_files.redact_patch(worktree.diff_patch(wt))
        summary = result_files.collect_test_summary(wt)
        _submit_result(cfg, attempt_id, nonce, result, diff, duration,
                       files=snapshot, diff_patch=patch or None,
                       test_summary=summary)
        return {"exit_code": result.exit_code}
    except Exception as exc:
        lease_lost.set()
        result = adapters.AdapterResult(
            exit_code=125, log_tail=f"runner 内部错误: {type(exc).__name__}: {exc}"[:500])
        _submit_result(cfg, attempt_id, nonce, result, "", time.monotonic() - started)
        return {"exit_code": 125}
    finally:
        lease_lost.set()
        if wt is not None:
            worktree.cleanup_worktree(project, wt, task_id)


def poll_once(cfg):
    """单轮：先重传缓存结果，再 poll；有任务则执行。返回是否有任务。

    轮询请求网络失败（post_json 返回 status 0）抛 RunnerPollError，
    由 main() 转为指数退避——这是全局 5s→60s 退避约束的实现点。
    """
    flush_pending(cfg)
    status, data = post_json(cfg, "/api/commands/poll", {"runner_id": cfg.runner_id})
    if status == 0:
        raise RunnerPollError(str(data.get("error", "网络不可达")))
    if status != 200 or not data.get("ok"):
        return False
    task = data.get("task")
    if not task:
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
    interval = args.interval or cfg.poll_interval_s

    if args.once:
        try:
            poll_once(cfg)
        except RunnerPollError as exc:
            print(f"[runner] hub 不可达: {exc}", file=sys.stderr)
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
            print(f"[runner] hub 不可达，{backoff}s 后重试: {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[runner] 轮询异常: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


if __name__ == "__main__":
    raise SystemExit(main())