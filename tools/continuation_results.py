"""Strict, bounded Git snapshots for one managed native continuation.

Snapshots use a temporary Git index and a directory-relative, no-follow reader;
they never modify the user's/worktree index, branch, refs, filters or hooks.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from fleet_core.process.bounded_process import run_bounded
from fleet_core.result_files import redact_patch
from fleet_core.task_result import MAX_DIFF_PATCH, MAX_TEST_SUMMARY, bound_patch, normalize_test_summary
from tools.spawn_policy import build_child_env
from tools.worktree import _source_file, _git_env, WorktreeError

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TREE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_MAX_FILES = 4096
_MAX_SOURCE_BYTES = 10 * 1024 * 1024
_MAX_STAT_BYTES = 5120


class ContinuationResultUnavailable(RuntimeError):
    """Exact worktree identity or a bounded Git snapshot could not be proven."""

    def __init__(self, code="result_unavailable"):
        self.code = code if code in {"result_unavailable", "result_too_large"} else "result_unavailable"
        super().__init__(self.code)


def _run(worktree: Path, git_dir: Path, index: Path, *args: str,
         limit: int = _MAX_SOURCE_BYTES) -> bytes:
    env = _git_env(git_dir, worktree)
    env["GIT_INDEX_FILE"] = str(index)
    try:
        result = run_bounded(
            ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
             "-c", "core.untrackedCache=false", "--no-replace-objects", *args],
            cwd=worktree, env=env, timeout_s=60,
            stdout_limit=limit + 1, stderr_limit=2048)
    except Exception as exc:
        raise ContinuationResultUnavailable() from exc
    if result.returncode != 0 or len(result.stdout) > limit:
        raise ContinuationResultUnavailable()
    return result.stdout


def _trusted_worktree(project: Path, task_id: str) -> tuple[Path, Path]:
    """Resolve only <configured project>.fleet-worktrees/<task_id> ownership."""
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ContinuationResultUnavailable()
    project = Path(project)
    if not project.is_absolute() or project.is_symlink() or not project.is_dir():
        raise ContinuationResultUnavailable()
    worktree = project.parent / f"{project.name}.fleet-worktrees" / task_id
    if worktree.parent.is_symlink() or worktree.is_symlink() or not worktree.is_dir():
        raise ContinuationResultUnavailable()
    if os.name != "nt" and (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd):
        raise ContinuationResultUnavailable()
    try:
        project_git = Path(os.fsdecode(_git_project_gitdir(project)))
        expected = project_git / "worktrees" / task_id
        if expected.parent.is_symlink() or expected.is_symlink() or not expected.is_dir():
            raise ContinuationResultUnavailable()
        marker = worktree / ".git"
        marker_stat = marker.lstat()
        if not stat.S_ISREG(marker_stat.st_mode):
            raise ContinuationResultUnavailable()
        marker_entry = _source_file(worktree, b".git", 8192)
        if marker_entry is None:
            raise ContinuationResultUnavailable()
        raw = marker_entry[0].decode("utf-8", errors="strict")
        if not raw.startswith("gitdir: ") or len(raw) > 8192:
            raise ContinuationResultUnavailable()
        linked = Path(raw[8:].strip())
        if not linked.is_absolute():
            linked = worktree / linked
        if linked.resolve(strict=True) != expected.resolve(strict=True):
            raise ContinuationResultUnavailable()
        # The configured project root plus deterministic worktree path is the
        # authority; do not follow an Agent-edited .git backlink.
        (project_git / "worktrees" / task_id).resolve(strict=True).relative_to(project_git.resolve(strict=True))
        return worktree, expected
    except ContinuationResultUnavailable:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise ContinuationResultUnavailable() from exc


def _git_project_gitdir(project: Path) -> bytes:
    """Get configured root's Git dir without consulting a worktree backlink."""
    env = build_child_env()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        import subprocess
        from fleet_core.process.local_process import run
        result = run(["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
                      "rev-parse", "--absolute-git-dir"], cwd=str(project), env=env,
                     capture_output=True, timeout=10, stdin=subprocess.DEVNULL, check=False)
    except Exception as exc:
        raise ContinuationResultUnavailable() from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise ContinuationResultUnavailable()
    return value


def snapshot_tree(project: Path, task_id: str) -> str:
    """Hash the exact current tracked + non-ignored worktree state."""
    worktree, git_dir = _trusted_worktree(project, task_id)
    with tempfile.TemporaryDirectory(prefix="fleet-continuation-index-") as temp:
        index = Path(temp) / "index"
        _run(worktree, git_dir, index, "read-tree", "HEAD")
        raw_paths = _run(worktree, git_dir, index,
                         "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                         limit=4 * 1024 * 1024)
        paths = tuple(dict.fromkeys(path for path in raw_paths.split(b"\0") if path))
        if len(paths) > _MAX_FILES:
            raise ContinuationResultUnavailable("result_too_large")
        index_info = bytearray()
        _run(worktree, git_dir, index, "read-tree", "--empty")
        total = 0
        for encoded in paths:
            try:
                item = _source_file(worktree, encoded, _MAX_SOURCE_BYTES - total)
            except Exception as exc:
                code = "result_too_large" if isinstance(exc, WorktreeError) and str(exc) == "source_archive_too_large" else "result_unavailable"
                raise ContinuationResultUnavailable(code) from exc
            if item is None:
                continue
            content, mode = item
            total += len(content)
            if total > _MAX_SOURCE_BYTES:
                raise ContinuationResultUnavailable("result_too_large")
            oid = _hash_source(worktree, git_dir, index, content)
            index_info.extend(mode.encode("ascii") + b" " + oid + b"\t" + encoded + b"\0")
        _run_input(worktree, git_dir, index, "update-index", "-z", "--index-info", input_bytes=bytes(index_info))
        tree = _run(worktree, git_dir, index, "write-tree", limit=128).decode("ascii").strip()
    if not _TREE.fullmatch(tree):
        raise ContinuationResultUnavailable()
    return tree


def _hash_source(worktree: Path, git_dir: Path, index: Path, content: bytes) -> bytes:
    """Write one bounded blob through the existing safe local process wrapper."""
    from fleet_core.process.local_process import run
    env = _git_env(git_dir, worktree)
    env["GIT_INDEX_FILE"] = str(index)
    try:
        result = run(["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
                      "hash-object", "--no-filters", "-w", "--stdin"], cwd=str(worktree),
                     env=env, input=content, capture_output=True, timeout=30, check=False)
    except Exception as exc:
        raise ContinuationResultUnavailable() from exc
    value = result.stdout.strip()
    if result.returncode != 0 or not _TREE.fullmatch(value.decode("ascii", errors="ignore")):
        raise ContinuationResultUnavailable()
    return value


def _run_input(worktree: Path, git_dir: Path, index: Path, *args: str, input_bytes: bytes) -> None:
    from fleet_core.process.local_process import run
    env = _git_env(git_dir, worktree)
    env["GIT_INDEX_FILE"] = str(index)
    try:
        result = run(["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args],
                     cwd=str(worktree), env=env, input=input_bytes, capture_output=True,
                     timeout=30, check=False)
    except Exception as exc:
        raise ContinuationResultUnavailable() from exc
    if result.returncode != 0:
        raise ContinuationResultUnavailable()


def compare_trees(project: Path, task_id: str, before: str, after: str) -> dict:
    if not _TREE.fullmatch(before) or not _TREE.fullmatch(after):
        raise ContinuationResultUnavailable()
    worktree, git_dir = _trusted_worktree(project, task_id)
    env = _git_env(git_dir, worktree)
    prefix = ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
              "--no-replace-objects"]
    try:
        stat_result = run_bounded([*prefix, "diff", "--no-ext-diff", "--no-textconv", "--stat", before, after], cwd=worktree,
                                  env=env, timeout_s=60, stdout_limit=_MAX_STAT_BYTES + 1,
                                  stderr_limit=2048)
        changed_result = run_bounded([*prefix, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", before, after],
                                     cwd=worktree, env=env, timeout_s=60,
                                     stdout_limit=4 * 1024 * 1024, stderr_limit=2048)
        patch_result = run_bounded([*prefix, "diff", "--no-color", "--no-ext-diff",
                                    "--no-textconv", before, after], cwd=worktree, env=env,
                                   timeout_s=60, stdout_limit=MAX_DIFF_PATCH + 1,
                                   stderr_limit=2048)
    except Exception as exc:
        raise ContinuationResultUnavailable() from exc
    if stat_result.returncode != 0 or changed_result.returncode != 0 or patch_result.returncode != 0:
        raise ContinuationResultUnavailable()
    stat_text, stat_truncated = bound_patch(stat_result.stdout.decode("utf-8", errors="replace"), _MAX_STAT_BYTES)
    patch, patch_truncated = bound_patch(patch_result.stdout.decode("utf-8", errors="replace"), MAX_DIFF_PATCH)
    patch, redacted = redact_patch(patch, MAX_DIFF_PATCH)
    summary = None
    summary_unavailable = False
    changed_paths = {path.decode("utf-8", errors="replace")
                     for path in changed_result.stdout.split(b"\0") if path}
    for name in ("test-results.json", "test_results.json"):
        if name not in changed_paths:
            continue
        try:
            # Read the immutable after-tree blob, never a later file that an
            # Agent may replace after snapshot (or an unchanged prior report).
            with tempfile.TemporaryDirectory(prefix="fleet-result-summary-") as temp:
                index = Path(temp) / "index"
                exists = _run(worktree, git_dir, index, "ls-tree", "-z", after, "--", name, limit=1024)
                if not exists:
                    continue
                report = _run(worktree, git_dir, index, "show", after + ":" + name, limit=MAX_TEST_SUMMARY)
            summary = normalize_test_summary(report.decode("utf-8", errors="strict"))
            if summary is None:
                raise ValueError("invalid_test_summary")
            break
        except Exception:
            summary_unavailable = True
    return {"before_tree": before, "after_tree": after, "diff_stat": stat_text,
            "diff_patch": patch, "diff_truncated": patch_truncated,
            "stat_truncated": stat_truncated, "redacted": redacted,
            "test_summary": summary, "summary_unavailable": summary_unavailable}


__all__ = ["ContinuationResultUnavailable", "compare_trees", "snapshot_tree"]
