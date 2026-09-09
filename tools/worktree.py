"""tools/worktree.py — 任务级 git worktree 隔离

每个任务在 <project>.fleet-worktrees/<task_id> 建独立 worktree + fleet/<task_id> 分支，
不污染原工作区；结束后 best-effort 清理。
"""
import re
import subprocess
from pathlib import Path

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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
    encoded = out.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return out
    cut = encoded[:max_bytes].decode("utf-8", "replace")
    return cut + "\n…[truncated]"


def diff_stat(worktree: Path, max_bytes=5120) -> str:
    worktree = Path(worktree)
    _git(worktree, "add", "-A")  # 让未跟踪文件进入 diff 视野（worktree 即用即弃）
    proc = _git(worktree, "diff", "--cached", "--stat")
    return _truncate_text(proc.stdout, max_bytes)


def diff_patch(worktree: Path, max_bytes=102400) -> str:
    """Unified diff of the disposable worktree. Caller redacts."""
    worktree = Path(worktree)
    _git(worktree, "add", "-A")
    proc = _git(worktree, "diff", "--cached", "--no-color", "--binary")
    return _truncate_text(proc.stdout or "", max_bytes)


def cleanup_worktree(project: Path, worktree: Path, task_id: str) -> None:
    try:
        _git(Path(project), "worktree", "remove", "--force", str(worktree), check=False)
        _git(Path(project), "branch", "-D", f"fleet/{task_id}", check=False)
    except Exception:
        pass