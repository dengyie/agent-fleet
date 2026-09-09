"""Bounded, allowlisted task-result file snapshots.

Runner collects a small set of text files from the disposable worktree and
attaches them to ``POST /api/commands/<id>/result``. Hub re-validates the same
rules, stores the snapshot locally, and serves ``GET /api/tasks/<id>/files/...``
without reverse-connecting the runner.

Rules (fail closed):

- relative POSIX path only; no ``..``, absolute, backslash, empty, or ``//``;
- depth / name / suffix allowlist;
- secret-looking names rejected (``.env``, ``id_rsa``, ``credentials``, ...);
- symlink / NUL / non-UTF8 skipped at collection time;
- per-file byte + line caps, per-result file-count cap;
- content redacted with the session redactor (passthrough off);
- operator read is rate-limited by the Hub service (see ``MAX_READS_PER_WINDOW``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from tools.session.redact import RedactionReport, Redactor

MAX_FILES = 16
MAX_FILE_BYTES = 16384
MAX_FILE_LINES = 400
MAX_DEPTH = 4
MAX_NAME = 64
MAX_READS_PER_WINDOW = 20
READ_WINDOW_S = 60.0
MAX_DIFF_PATCH = 102400
TEST_SUMMARY_KEYS = (
    "framework", "passed", "failed", "skipped", "errors", "duration_s", "failed_names",
)
TEST_RESULT_NAMES = ("test-results.json", "test_results.json")

ALLOWED_SUFFIXES = (
    ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml",
    ".rst", ".ini", ".cfg", ".js", ".ts", ".css", ".html", ".sh",
)

_FORBIDDEN_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "credentials", "credentials.json", "credential",
    "secrets", "secrets.json", "secret",
    "authorized_keys", "known_hosts",
})

_FORBIDDEN_PARTS = (
    ".ssh", "id_rsa", "id_ed25519", "credentials", "credential",
    ".netrc", "secret", "token", "passwd", ".aws",
)


class PathRejected(ValueError):
    """Path failed the allowlist. Public callers must not echo the raw path."""


def normalize_path(raw: Any) -> str:
    """Return a relative POSIX path or raise :class:`PathRejected`."""
    if not isinstance(raw, str) or not raw or raw in (".", ".."):
        raise PathRejected("rejected")
    if "\\" in raw or "//" in raw or raw.startswith("/") or raw.startswith("~"):
        raise PathRejected("rejected")
    parts = raw.split("/")
    if len(parts) > MAX_DEPTH:
        raise PathRejected("rejected")
    for part in parts:
        if not part or part in (".", "..") or len(part) > MAX_NAME:
            raise PathRejected("rejected")
        lowered = part.lower()
        if lowered in _FORBIDDEN_NAMES:
            raise PathRejected("rejected")
        if part.startswith("."):
            raise PathRejected("rejected")
        if any(token in lowered for token in _FORBIDDEN_PARTS):
            raise PathRejected("rejected")
    lowered_path = raw.lower()
    if not any(lowered_path.endswith(suffix) for suffix in ALLOWED_SUFFIXES):
        raise PathRejected("rejected")
    return "/".join(parts)


def bound_content(
    text: str,
    max_bytes: int = MAX_FILE_BYTES,
    max_lines: int = MAX_FILE_LINES,
) -> tuple[str, bool]:
    """Truncate ``text`` to ``max_lines`` then ``max_bytes``. Never raises."""
    if not isinstance(text, str):
        text = str(text)
    truncated = False
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        text = "".join(lines[:max_lines])
        truncated = True
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, truncated
    cut = encoded[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True


def redact_patch(text: str, max_bytes: int = MAX_DIFF_PATCH) -> tuple[str, bool]:
    """Redact + bound a unified diff. Empty input stays empty."""
    if not text:
        return "", False
    redacted, report = redact_content(text)
    encoded = redacted.encode("utf-8")
    truncated = len(encoded) > max_bytes
    if truncated:
        cut = encoded[:max_bytes]
        while cut:
            try:
                redacted = cut.decode("utf-8")
                break
            except UnicodeDecodeError:
                cut = cut[:-1]
        else:
            redacted = ""
        redacted += "\n…[truncated]"
    return redacted, truncated or report.replaced > 0


def normalize_test_summary(raw: Any) -> dict[str, Any] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in TEST_SUMMARY_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "framework":
            text = str(value)[:32]
            out[key] = text if text in ("pytest", "unittest", "unknown") else "unknown"
        elif key in ("passed", "failed", "skipped", "errors"):
            try:
                out[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        elif key == "duration_s":
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
        elif key == "failed_names" and isinstance(value, (list, tuple)):
            names = [str(item)[:200] for item in value[:20] if str(item)]
            out[key] = names
    return out or None


def collect_test_summary(worktree: Path) -> dict[str, Any] | None:
    """Read an optional allowlisted test-results.json from the worktree root."""
    root = Path(worktree)
    for name in TEST_RESULT_NAMES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return normalize_test_summary(raw)
    return None


def redact_content(text: str) -> tuple[str, RedactionReport]:
    """Redact secret sigils in file text. Original never appears in the report."""
    if not isinstance(text, str):
        text = str(text)
    redactor = Redactor(passthrough=False)
    sanitized, report = redactor.redact_event({"payload": {"body": text}})
    payload = sanitized.get("payload") if isinstance(sanitized, dict) else None
    body = payload.get("body") if isinstance(payload, dict) else text
    if not isinstance(body, str):
        body = str(body)
    return body, report


def _record(path: str, content: str, truncated: bool, redacted: bool) -> dict[str, Any]:
    return {
        "path": path,
        "content": content,
        "truncated": bool(truncated),
        "redacted": bool(redacted),
        "bytes": len(content.encode("utf-8")),
    }


def sanitize_files(files: Any, *, max_files: int = MAX_FILES) -> list[dict[str, Any]]:
    """Re-validate a runner-supplied file list. Unknown/unsafe entries drop out."""
    if not isinstance(files, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if len(out) >= max_files:
            break
        if not isinstance(item, dict):
            continue
        try:
            path = normalize_path(item.get("path"))
        except PathRejected:
            continue
        if path in seen:
            continue
        content = item.get("content")
        if not isinstance(content, str) or "\x00" in content:
            continue
        redacted_text, report = redact_content(content)
        bounded, truncated = bound_content(redacted_text)
        seen.add(path)
        out.append(_record(path, bounded, truncated, report.replaced > 0))
    return out


def collect_result_files(
    worktree: Path,
    max_files: int = MAX_FILES,
) -> list[dict[str, Any]]:
    """Walk a worktree and return a bounded, redacted snapshot (no symlinks)."""
    root = Path(worktree)
    try:
        root = root.resolve()
    except OSError:
        return []
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    try:
        candidates: Iterable[Path] = sorted(root.rglob("*"))
    except OSError:
        return []
    for path in candidates:
        if len(found) >= max_files:
            break
        try:
            if not path.is_file() or path.is_symlink():
                continue
        except OSError:
            continue
        if ".git" in path.parts:
            continue
        try:
            rel = path.relative_to(root).as_posix()
            norm = normalize_path(rel)
        except (PathRejected, ValueError):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        if len(raw) > MAX_FILE_BYTES * 4:
            raw = raw[:MAX_FILE_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        redacted_text, report = redact_content(text)
        bounded, truncated = bound_content(redacted_text)
        found.append(_record(norm, bounded, truncated, report.replaced > 0))
    return found


def public_file_meta(item: dict[str, Any]) -> dict[str, Any]:
    """List-row DTO: path + bounds flags, never the body."""
    return {
        "path": item.get("path", ""),
        "bytes": int(item.get("bytes") or 0),
        "truncated": bool(item.get("truncated")),
        "redacted": bool(item.get("redacted")),
    }


def public_file(item: dict[str, Any]) -> dict[str, Any]:
    """Single-file DTO for the operator read surface."""
    content = item.get("content")
    if not isinstance(content, str):
        content = ""
    content, truncated = bound_content(content)
    return {
        "path": item.get("path", ""),
        "content": content,
        "bytes": int(item.get("bytes") or len(content.encode("utf-8"))),
        "truncated": bool(item.get("truncated")) or truncated,
        "redacted": bool(item.get("redacted")),
    }


__all__ = [
    "ALLOWED_SUFFIXES",
    "MAX_DEPTH",
    "MAX_FILE_BYTES",
    "MAX_FILE_LINES",
    "MAX_FILES",
    "MAX_READS_PER_WINDOW",
    "PathRejected",
    "READ_WINDOW_S",
    "bound_content",
    "collect_result_files",
    "collect_test_summary",
    "normalize_path",
    "normalize_test_summary",
    "redact_patch",
    "public_file",
    "public_file_meta",
    "redact_content",
    "sanitize_files",
]
