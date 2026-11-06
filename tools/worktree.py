"""tools/worktree.py — 任务级 git worktree 隔离

每个任务在 <project>.fleet-worktrees/<task_id> 建独立 worktree + fleet/<task_id> 分支，
不污染原工作区；结束后 best-effort 清理。
"""
import os
import re
import subprocess
import threading
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
