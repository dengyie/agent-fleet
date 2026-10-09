"""tools/worktree.py — 任务级 git worktree 隔离

每个任务在 <project>.fleet-worktrees/<task_id> 建独立 worktree + fleet/<task_id> 分支，
不污染原工作区；结束后 best-effort 清理。
"""
import re
import subprocess
from pathlib import Path

from hub.domain.task_result import MAX_DIFF_PATCH, bound_patch
from tools.bounded_process import run_bounded

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DIFF_TIMEOUT_S = 60.0


class WorktreeError(RuntimeError):
    pass


def _git(project: Path, *args, check=True):
    try:
        proc = subprocess.run(["git", *args], cwd=str(project),
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
    _git(project, "worktree", "add", str(target), "-b", f"fleet/{task_id}")
    return target


def _truncate_text(out: str, max_bytes: int) -> str:
    return bound_patch(out, max_bytes)[0]


def _git_diff(worktree: Path, *args: str, max_bytes: int) -> str:
    """Drain both pipes under one deadline, retaining only a bounded prefix."""
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_DIFF_PATCH:
        raise WorktreeError("invalid_diff_limit")
    command = ["git", "diff", "--cached", "--no-ext-diff", "--no-textconv", *args]
    try:
        proc = run_bounded(command, cwd=worktree, timeout_s=DIFF_TIMEOUT_S,
                           stdout_limit=max_bytes + 1, stderr_limit=1200)
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
    try:
        _git(Path(project), "worktree", "remove", "--force", str(worktree), check=False)
        _git(Path(project), "branch", "-D", f"fleet/{task_id}", check=False)
    except Exception:
        pass
