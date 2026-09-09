"""JSONL observation repository extracted from the legacy ``hub.state`` module.

Owns the persistence semantics unchanged:

* per-machine ``RLock`` serializes access for one machine
* ``state/<machine>.jsonl`` is appended with one JSON snapshot per line; each
  snapshot is stamped with ``_ts`` in-place before serialization
* oversized JSONL files rotate by atomically replacing the file with the tail
  ``keep_lines`` rows (``NamedTemporaryFile`` + ``flush`` + ``fsync`` +
  ``os.replace``)
* ``state/<machine>.json`` holds the latest snapshot as pretty JSON, replaced
  atomically
* malformed lines in the JSONL history are skipped silently
* machine names must match the auth machine-name contract so repository paths
  never escape the repository directory

``hub.state`` is a thin compatibility facade over a default instance of this
class constructed from the legacy ``STATE_DIR``; new services inject a
repository built on an explicit ``Path``.
"""

import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

from hub.repositories import MACHINE_NAME_RE, validate_machine_name

MAX_JSONL_BYTES = 5 * 1024 * 1024
ROTATE_KEEP_LINES = 2000


class JsonlObservationRepository:
    """Per-machine JSONL history with an atomic current-JSON mirror.

    Constructor-injected ``state_dir`` keeps the persistence layer free of
    module-level path lookups and the Flask stack.
    """

    def __init__(self, state_dir: Path, *, max_jsonl_bytes=MAX_JSONL_BYTES,
                 keep_lines=ROTATE_KEEP_LINES) -> None:
        self.state_dir = Path(state_dir)
        self.max_jsonl_bytes = int(max_jsonl_bytes)
        self.keep_lines = int(keep_lines)
        self._locks = defaultdict(threading.RLock)

    # -- internal helpers -------------------------------------------------

    def _mach_file(self, machine: str, ext: str = "jsonl") -> Path:
        return self.state_dir / f"{validate_machine_name(machine)}.{ext}"

    def _rotate_if_needed(self, machine: str, f: Path) -> bool:
        """Truncate an oversized JSONL to the tail ``keep_lines`` rows.

        Not holding a lock here by itself: callers hold the machine lock so
        the rotation and its atomic replace stay serialized.
        """
        try:
            if not f.exists() or f.stat().st_size <= self.max_jsonl_bytes:
                return False
        except OSError:
            return False
        try:
            lines = f.read_text().splitlines()
        except OSError:
            return False
        keep = lines[-self.keep_lines:]
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self.state_dir, prefix="rotate-",
            suffix=".tmp", delete=False,
        )
        tmp_name = tmp.name
        try:
            with tmp:
                tmp.write("\n".join(keep) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, f)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return True

    # -- ObservationRepository protocol -----------------------------------

    def save_snapshot(self, machine: str, snapshot: dict) -> None:
        """Append the snapshot to the JSONL and atomically replace ``current``.

        Mirrors the legacy behavior exactly: an in-place ``_ts`` stamp, the
        JSONL append, bounded rotation, and the atomic current-JSON replace.
        """
        validated = validate_machine_name(machine)
        with self._locks[validated]:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            snapshot["_ts"] = time.time()
            f = self._mach_file(validated)
            with open(f, "a") as fh:
                fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            self._rotate_if_needed(validated, f)

            target = self._mach_file(validated, "json")
            tmp = tempfile.NamedTemporaryFile(
                mode="w", dir=self.state_dir, prefix="snapshot-",
                suffix=".tmp", delete=False,
            )
            tmp_name = tmp.name
            try:
                with tmp:
                    json.dump(snapshot, tmp, ensure_ascii=False, indent=2)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, target)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass

    def read_current(self, machine: str) -> dict | None:
        """Return the most recent snapshot for ``machine`` or ``None``."""
        validated = validate_machine_name(machine)
        f = self._mach_file(validated, "json")
        if not f.exists():
            return None
        with self._locks[validated]:
            try:
                return json.loads(f.read_text())
            except Exception:
                return None

    def read_history(self, machine: str, limit: int = 50) -> list[dict]:
        """Return the newest (up to ``limit``) snapshots, oldest first."""
        validated = validate_machine_name(machine)
        f = self._mach_file(validated)
        if not f.exists():
            return []
        with self._locks[validated]:
            out = []
            for line in f.read_text().splitlines()[-limit:]:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def machines(self) -> set[str]:
        """Return the machine names that have a current snapshot.

        Stems that do not satisfy the machine-name contract are skipped:
        a stray malformed ``*.json`` artifact must never leak into machine
        enumeration and abort reconciliation.
        """
        try:
            return {
                p.stem for p in self.state_dir.glob("*.json")
                if MACHINE_NAME_RE.fullmatch(p.stem)
            }
        except OSError:
            return set()


class LegacyStateStoreAdapter:
    """Compatibility face for legacy ``hub.state`` callers over a repository.

    Keeps the old module-level function contract (including ``diff_previous``)
    working against an injected :class:`ObservationRepository` without
    duplicating persistence logic.
    """

    def __init__(self, repository) -> None:
        self.repository = repository

    def save_snapshot(self, machine: str, snapshot: dict) -> None:
        self.repository.save_snapshot(machine, snapshot)

    def read_current(self, machine: str) -> dict | None:
        return self.repository.read_current(machine)

    def read_history(self, machine: str, limit: int = 50) -> list[dict]:
        return self.repository.read_history(machine, limit=limit)

    def machines(self) -> set[str]:
        return self.repository.machines()

    def diff_previous(self, machine: str, snapshot: dict) -> list[str]:
        """Return the field names whose payload changed versus the last snapshot."""
        prev = self.read_current(machine)
        if prev is None:
            return list(snapshot.keys())
        changes = []
        for key in ("hermes", "agents", "system"):
            if key in snapshot and key in prev and (
                json.dumps(snapshot[key], sort_keys=True)
                != json.dumps(prev[key], sort_keys=True)
            ):
                changes.append(key)
        return changes
