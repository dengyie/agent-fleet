"""tools/worktree.py — 任务级 git worktree 隔离

每个任务在 <project>.fleet-worktrees/<task_id> 建独立 worktree + fleet/<task_id> 分支，
不污染原工作区；结束后 best-effort 清理。
"""
import os
import re
import subprocess
import threading
import tempfile
import asyncio
import time
import stat
from datetime import datetime, timezone
from typing import Callable, Mapping
from fleet_core.process.local_process import NO_WINDOW
from pathlib import Path

from fleet_core.task_result import MAX_DIFF_PATCH, bound_patch
from fleet_core.process.bounded_process import run_bounded
from fleet_core.process.local_process import run as run_local
from tools.spawn_policy import build_child_env

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DIFF_TIMEOUT_S = 60.0


class WorktreeError(RuntimeError):
    pass


#: worktree 内容由 agent 控制：禁用 fsmonitor/hooks/untracked cache，
#: 防止被篡改的配置在 runner 收尾（diff/add）时执行任意程序。
_GIT_HARDEN = (
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",   # POSIX /dev/null，Windows 为 nul
    "-c", "core.untrackedCache=false",
)

#: create_worktree 时登记的「worktree → 真实 gitdir」。之后一律显式 GIT_DIR，
#: 不再信任 worktree 里可被 agent 改写的 .git 指针文件。
_TRUSTED_GITDIRS: dict[str, Path] = {}
_TRUSTED_LOCK = threading.Lock()


def _key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _git_env(git_dir: Path | None, work_tree: Path) -> dict[str, str]:
    env = build_child_env()
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
        env["GIT_WORK_TREE"] = str(work_tree)
    return env


def _git_dir_for(path: Path, *, trusted_project: bool = False) -> Path | None:
    """返回需要钉死的 gitdir；未登记且 .git 不是普通目录的 worktree 一律拒绝。"""
    path = Path(path)
    with _TRUSTED_LOCK:
        registered = _TRUSTED_GITDIRS.get(_key(path))
    if registered is not None:
        return registered
    dot_git = path / ".git"
    if dot_git.is_dir() and not dot_git.is_symlink():
        return dot_git.resolve()
    if trusted_project:
        return None  # 运营方配置的项目仓库（自身可能是 worktree）：交给 git 自行解析
    raise WorktreeError(f"untrusted_gitdir: {path}")


def _git(project: Path, *args, check=True, trusted_project=False):
    project = Path(project)
    git_dir = _git_dir_for(project, trusted_project=trusted_project)
    try:
        proc = run_local(["git", *_GIT_HARDEN, *args], cwd=str(project),
                         env=_git_env(git_dir, project),
                         capture_output=True, text=True, errors="replace",
                         timeout=60)
    except subprocess.TimeoutExpired:
        raise WorktreeError(f"git {' '.join(args)} 超时")
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} 失败: {proc.stderr.strip()[:300]}")
    return proc


def _root(project: Path) -> Path:
    return project.parent / f"{project.name}.fleet-worktrees"


def create_worktree(project: Path, task_id: str) -> Path:
    project = Path(project)
    if not TASK_ID_RE.fullmatch(task_id):
        raise WorktreeError(f"非法 task_id: {task_id!r}")
    if not (project / ".git").exists():
        raise WorktreeError(f"{project} 不是 git 仓库")
    target = _root(project) / task_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        cleanup_worktree(project, target, task_id)
    _git(project, "worktree", "add", str(target), "-b", f"fleet/{task_id}",
         trusted_project=True)
    # agent 启动前登记真实 gitdir（此刻 .git 指针文件仍由 git 自己写入、可信）
    resolved = _git(target, "rev-parse", "--absolute-git-dir",
                    trusted_project=True).stdout.strip()
    if not resolved:
        raise WorktreeError("无法解析 worktree gitdir")
    with _TRUSTED_LOCK:
        _TRUSTED_GITDIRS[_key(target)] = Path(resolved)
    return target


def head_commit(worktree: Path) -> str:
    value = _git(Path(worktree), "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise WorktreeError("invalid_worktree_head")
    return value


async def _source_git(argv: list[str], cwd: Path, environment: Mapping[str, str], input_bytes: bytes | None,
                      deadline: float, should_cancel: Callable[[], bool] | None) -> bytes:
    process = None
    tasks: list[asyncio.Task] = []
    try:
        if time.monotonic() >= deadline or (should_cancel is not None and should_cancel()):
            raise WorktreeError("source_capture_stopped")
        process = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=environment,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)

        async def read(stream: asyncio.StreamReader, limit: int) -> bytes:
            chunks = bytearray()
            while True:
                chunk = await stream.read(min(65536, limit - len(chunks) + 1))
                if not chunk:
                    return bytes(chunks)
                chunks.extend(chunk)
                if len(chunks) > limit:
                    raise WorktreeError("source_git_output_limit")

        async def write() -> None:
            if input_bytes is None:
                return
            try:
                for offset in range(0, len(input_bytes), 65536):
                    process.stdin.write(memoryview(input_bytes)[offset:offset + 65536])
                    await process.stdin.drain()
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read(process.stdout, 4 * 1024 * 1024)),
                 asyncio.create_task(read(process.stderr, 8192)), asyncio.create_task(write()),
                 asyncio.create_task(process.wait())]
        pending = set(tasks)
        while pending:
            if time.monotonic() >= deadline or (should_cancel is not None and should_cancel()):
                raise WorktreeError("source_capture_stopped")
            done, pending = await asyncio.wait(pending, timeout=min(.1, max(0, deadline - time.monotonic())),
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        if process.returncode != 0:
            raise WorktreeError("source_git_failed: " + tasks[1].result().decode("utf-8", errors="replace")[:200])
        return tasks[0].result()
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                await process.wait()
            await asyncio.shield(process.wait())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _source_file(root: Path, encoded: bytes, remaining: int) -> tuple[bytes, str] | None:
    """Open every component relative to pinned, non-link directory descriptors."""
    from fleet_core.workflow.source import _parts

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise WorktreeError("source_file_boundary_unavailable")
    parts = _parts(encoded.decode("utf-8", errors="strict"))
    directory_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        try:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise WorktreeError("unsupported_source_entry")
            raw = handle.read(remaining + 1)
            if len(raw) > remaining:
                raise WorktreeError("source_archive_too_large")
            return raw, "100755" if info.st_mode & 0o111 else "100644"
    finally:
        os.close(directory)


def freeze_source_commit(worktree: Path, base_commit: str, *, task_id: str, timestamp: float,
                         object_directory: Path, deadline: float | None = None,
                         should_cancel: Callable[[], bool] | None = None) -> tuple[str, str]:
    """Build a fixed task-output commit using a private index and no branch update."""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_commit) or not TASK_ID_RE.fullmatch(task_id):
        raise WorktreeError("invalid_source_identity")
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    deadline = time.monotonic() + 120 if deadline is None else deadline
    environment = {
        **_git_env(_git_dir_for(Path(worktree)), Path(worktree)),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_AUTHOR_NAME": "Agent Fleet Source Exporter",
        "GIT_AUTHOR_EMAIL": "source-exporter@invalid",
        "GIT_COMMITTER_NAME": "Agent Fleet Source Exporter",
        "GIT_COMMITTER_EMAIL": "source-exporter@invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_OBJECT_DIRECTORY": str(Path(object_directory).resolve()),
    }
    try:
        Path(object_directory).mkdir(parents=True, exist_ok=True)
        objects = asyncio.run(_source_git(["git", "rev-parse", "--git-path", "objects"], Path(worktree),
            {key: value for key, value in environment.items() if key != "GIT_OBJECT_DIRECTORY"}, None, deadline, should_cancel)).decode().strip()
        alternate = (Path(worktree) / objects).resolve()
        environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(alternate)
        with tempfile.TemporaryDirectory(prefix="fleet-source-index-") as temp_name:
            index = Path(temp_name) / "index"
            environment["GIT_INDEX_FILE"] = str(index)

            def git(*args: str, input_bytes: bytes | None = None) -> bytes:
                return asyncio.run(_source_git(["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
                    "-c", "gc.auto=0", "--no-replace-objects", *args], Path(worktree), environment, input_bytes, deadline, should_cancel))

            paths = tuple(dict.fromkeys(part for part in git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0") if part))
            if len(paths) > 4096:
                raise WorktreeError("source_file_limit")
            git("read-tree", "--empty")
            records = bytearray()
            total = 0
            for encoded in paths:
                entry = _source_file(Path(worktree), encoded, 10 * 1024 * 1024 - total)
                if entry is None:
                    continue
                raw, mode = entry
                total += len(raw)
                oid = git("hash-object", "--no-filters", "-w", "--stdin", input_bytes=raw).strip()
                records.extend(mode.encode("ascii") + b" " + oid + b"\t" + encoded + b"\0")
            git("update-index", "-z", "--index-info", input_bytes=bytes(records))
            tree = git("write-tree").decode().strip()
            commit = git("commit-tree", tree, "-p", base_commit, "-m", f"Fleet task output {task_id}").decode().strip()
            return commit, tree
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise WorktreeError("source_commit_failed") from exc


def _truncate_text(out: str, max_bytes: int) -> str:
    return bound_patch(out, max_bytes)[0]


def _git_diff(worktree: Path, *args: str, max_bytes: int) -> str:
    """Drain both pipes under one deadline, retaining only a bounded prefix."""
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_DIFF_PATCH:
        raise WorktreeError("invalid_diff_limit")
    git_dir = _git_dir_for(worktree)
    command = ["git", *_GIT_HARDEN, "diff", "--cached", "--no-ext-diff", "--no-textconv", *args]
    try:
        proc = run_bounded(command, cwd=worktree, timeout_s=DIFF_TIMEOUT_S,
                           stdout_limit=max_bytes + 1, stderr_limit=1200,
                           env=_git_env(git_dir, Path(worktree)))
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError("git diff 超时") from exc
    except OSError as exc:
        raise WorktreeError("git diff 执行失败") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace")[:300].strip()
        raise WorktreeError(f"git diff 失败: {detail}")
    return _truncate_text(proc.stdout.decode("utf-8", errors="replace"), max_bytes)


def diff_stat(worktree: Path, max_bytes: int = 5120) -> str:
    worktree = Path(worktree)
    _git(worktree, "add", "-A")  # 让未跟踪文件进入 diff 视野（worktree 即用即弃）
    return _git_diff(worktree, "--stat", max_bytes=max_bytes)


def diff_patch(worktree: Path, max_bytes: int = MAX_DIFF_PATCH) -> str:
    """Unified diff of the disposable worktree. Caller redacts."""
    worktree = Path(worktree)
    _git(worktree, "add", "-A")
    return _git_diff(worktree, "--no-color", "--binary", max_bytes=max_bytes)


def cleanup_worktree(project: Path, worktree: Path, task_id: str) -> None:
    with _TRUSTED_LOCK:
        _TRUSTED_GITDIRS.pop(_key(worktree), None)
    try:
        _git(Path(project), "worktree", "remove", "--force", str(worktree), check=False,
             trusted_project=True)
        _git(Path(project), "branch", "-D", f"fleet/{task_id}", check=False,
             trusted_project=True)
    except Exception:
        pass
