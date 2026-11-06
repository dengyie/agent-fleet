"""Bounded encrypted local spool for session events (Task 4).

A durable, append-only, quota-bounded local buffer that stores validated,
redacted events as encrypted segments on the Agent.  It is the Agent-side
durable staging surface before ``tools.session.uploader`` ships bounded
batches to the Hub.

Design rules bound here (see the Task 4 brief):

- ``append(event)`` accepts only already-validated events
  (:func:`session_schema.validate_event`); anything else raises
  :class:`ValueError` without leaking the failing content.
- A sequence is assigned before persistence; the checkpoint records
  ``last_sequence`` and is fsynced (temp file + fsync + atomic rename +
  directory fsync) BEFORE ``append`` reports the event durable.
- ``exact``/:class:`structured` events that would overflow the session or
  machine byte quota are NEVER silently discarded and NEVER partially
  written: ``append`` returns an :class:`AppendResult` with
  ``capture_blocked=True`` (the explicit pause signal).
- A ``best_effort`` event that would overflow is surfaced as an explicit
  ``capture_gap`` signal in the sequence space (a tiny ``capture_gap``
  marker is persisted when it fits); it is never misclaimed as an exact
  capacity, and it is never silently dropped without a signal.
- Segments are bounded files sealed with atomic write-then-rename.  Each
  record is a frame: ``u64 seq | u32 enc_len | nonce||AEAD(body, ad)``.
  The AEAD tag is a per-record checksum: a corrupt/truncated frame only
  stops playback of that ONE segment; later segments stay replayable.
- ``ack(sequence)`` advances the acknowledged cursor durably and compacts
  (deletes) fully-acked sealed segments; replay never re-sends acked records.
- ``read_after`` is strictly bounded by ``limit`` and ``max_bytes`` and also
  by the ack cursor; it never loads an unbounded amount of data.
- Public status/checkpoint surfaces are bounded and leak-free: no
  filesystem paths, no exception text, no raw event payloads; internal
  diagnostics are stable short codes.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from session_schema import validate_event
from fleet_core.session.crypto import AeadBox

try:  # pragma: no cover - the fallback is exercised on non-POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - Windows-only owner lock primitive.
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

# Stable diagnostic codes (never raw paths / exception text / payloads).
CODE_SEGMENT_BAD = "segment_corrupt"
CODE_CHECKPOINT_BAD = "checkpoint_corrupt"
CODE_KEY_MISSING = "spool_key_missing"
CODE_SETUP = "spool_setup"
CODE_WRITE = "spool_write_failed"
CODE_BUSY = "spool_busy"

# Segment format versions.  v1 frames authenticate only the sequence; v2
# frames additionally bind the session id into the AEAD associated data so a
# frame copied into another session's segment fails authentication.  The
# segment magic selects the associated-data scheme, so v1 segments written by
# older builds stay readable (and a v1 crash-survivor active segment keeps
# being appended with v1 frames until it is sealed).
_MAGIC_V1 = b"SP4SEG01"
_MAGIC = b"SP4SEG02"
_MAGICS = (_MAGIC_V1, _MAGIC)
_HEADER_SIZE = len(_MAGIC) + 8  # magic + reserved u64
_FRAME_HEADER = 12              # u64 seq + u32 enc_len
_SEGMENT_TARGET = 262144        # 256 KiB per segment when full
MAX_EVENT_BYTES = 65536         # shared single-event bound (post-redaction)

_IS_WINDOWS = os.name == "nt"
_OWNER_LOCK_NAME = ".owner.lock"
_OWNER_POLL_S = 0.05
#: Default bound on waiting for another holder of a spool's owner lock.
OWNER_WAIT_S = 60.0
_QUOTA_LOCKS: dict[str, threading.RLock] = {}
_QUOTA_LOCKS_GUARD = threading.Lock()


def _thread_quota_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _QUOTA_LOCKS_GUARD:
        lock = _QUOTA_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _QUOTA_LOCKS[key] = lock
        return lock


def _frame_ad(version: bytes, session_id: str, seq: int) -> bytes:
    """AEAD associated data for one frame of a ``version`` segment."""
    if version == _MAGIC_V1:
        return str(seq).encode("ascii")
    return b"fleet-spool-v2\0" + session_id.encode("utf-8") + b"\0" + \
        str(seq).encode("ascii")


def _segment_pattern(session_id: str):
    """Exact segment-name matcher for ONE session (never a prefix match:
    session ``adopt`` must not claim ``adopt_ab``'s segments)."""
    return re.compile(rf"^{re.escape(session_id)}_(\d{{6,}})\.(active|sealed)$")


def _try_lock(handle) -> bool:
    """Non-blocking exclusive lock on ``handle``; False when contended."""
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except (BlockingIOError, PermissionError):
        return False
    except OSError as exc:
        # EACCES/EAGAIN/EDEADLK are contention on every supported platform.
        if exc.errno in (11, 13, 35, 36):
            return False
        raise


def _unlock(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def acquire_owner_lock(root, *, timeout: float = OWNER_WAIT_S):
    """Take the per-spool-root writer ownership lock.

    The live writer (a runner capture or native sync) holds it for the
    spool's lifetime so a concurrent replay never opens, appends to, seals or
    compacts a spool another process is writing.  Waits at most ``timeout``
    seconds (``0`` = try once) and then raises :class:`SpoolBusy`.  Returns
    an opaque handle for :func:`release_owner_lock`.
    """
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = open(root / _OWNER_LOCK_NAME, "a+b")
    except OSError as exc:
        raise SpoolError(CODE_SETUP) from exc
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise SpoolBusy()
            time.sleep(_OWNER_POLL_S)
    except SpoolBusy:
        handle.close()
        raise
    except OSError as exc:
        handle.close()
        raise SpoolError(CODE_SETUP) from exc
    return handle


def release_owner_lock(handle) -> None:
    if handle is not None:
        _unlock(handle)


def owner_lock_held(root) -> bool:
    """True when another holder owns ``root``'s writer lock right now."""
    root = Path(root)
    if not (root / _OWNER_LOCK_NAME).exists():
        return False
    try:
        handle = acquire_owner_lock(root, timeout=0)
    except SpoolBusy:
        return True
    except SpoolError:
        return False
    release_owner_lock(handle)
    return False


@contextmanager
def _quota_guard(root: Path):
    """Serialize quota admission across sessions and processes.

    The thread lock closes the same-process gap; the advisory file lock
    extends the critical section to independently running bridge processes.
    Only the short check/write/checkpoint window is held.
    """
    lock = _thread_quota_lock(root)
    lock.acquire()
    handle = None
    try:
        if fcntl is not None:
            try:
                handle = open(root / ".machine-quota.lock", "a+b")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                if handle is not None:
                    handle.close()
                    handle = None
                raise SpoolError(CODE_SETUP) from exc
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        lock.release()


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_size(event: dict) -> int:
    return len(json.dumps(event, ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8"))


def _frame_footprint(body_size: int) -> int:
    """Encrypted on-disk footprint of a plaintext body in a frame."""
    return _FRAME_HEADER + (12 + body_size + 16)


def _build_gap_event(dropped: dict, seq: int) -> dict:
    """A validated ``capture_gap`` marker replacing one dropped event."""
    event = {
        "schema_version": 1,
        "event_id": f"gap_{seq:08x}",
        "stream_id": dropped.get("stream_id") or "stream",
        "machine_id": dropped.get("machine_id") or "host",
        "session_id": dropped.get("session_id") or "session",
        "sequence": seq,
        "kind": "capture_gap",
        "capture_quality": "structured",
        "emitted_at": dropped.get("emitted_at") or _now_rfc3339(),
        "payload": {
            "start_sequence": seq,
            "end_sequence": seq,
            "quality": "best_effort",
            "reason": "spool_quota",
        },
    }
    return validate_event(event)


@dataclass(frozen=True)
class AppendResult:
    """Result of a single :meth:`LocalSpool.append`.

    Fields (frozen/immutable; bounded; no paths / exception text / payloads):

    - ``sequence`` — durable sequence number of the stored event (an ordinary
      accept), or ``None`` when the exact/structured event could not be
      stored.
    - ``capture_blocked`` — ``True`` exactly for a quota-exhaustion pause on
      an exact/structured event that was NOT written.  It is a distinct,
      explicit signal, never conflated with a normal sequence number.
    - ``gap_sequence`` — when a best-effort event was dropped under quota, the
      sequence assigned to the ``capture_gap`` marker that replaced it
      (persisted when it fits, in-memory otherwise); ``None`` when no gap was
      recorded.
    """

    sequence: int | None
    capture_blocked: bool
    gap_sequence: int | None = None

    @property
    def accepted(self) -> bool:
        return self.sequence is not None


class SpoolError(RuntimeError):
    """Bounded spool error: only a stable short code is exposed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class SpoolBusy(SpoolError):
    """Another live writer owns this spool root (see owner lock)."""

    def __init__(self):
        super().__init__(CODE_BUSY)


class _FrameReader:
    """Incremental frame reader over ONE encrypted segment file.

    ``frames()`` yields ``(seq, plaintext)`` per complete, authenticated
    frame.  On a checksum/tag failure or truncated frame it stops playback
    for that segment (``corrupt=True``) — a corrupt segment never poisons the
    rest of the spool stream.
    """

    def __init__(self, path: Path, box: AeadBox, session_id: str):
        self.path = path
        self.box = box
        self.session_id = session_id
        self.corrupt = False

    def frames(self, after: int = 0):
        """Yield ``(seq, plaintext)``; frames with ``seq <= after`` are
        skipped by header only (seek past the ciphertext, never decrypted)."""
        try:
            handle = open(self.path, "rb")
        except OSError:
            self.corrupt = True
            return
        with handle:
            try:
                file_size = os.fstat(handle.fileno()).st_size
            except OSError:
                self.corrupt = True
                return
            head = handle.read(_HEADER_SIZE)
            version = head[: len(_MAGIC)]
            if version not in _MAGICS or len(head) != _HEADER_SIZE:
                self.corrupt = True
                return
            while True:
                raw_seq = handle.read(8)
                raw_len = handle.read(4)
                if len(raw_seq) == 0 and len(raw_len) == 0:
                    return  # clean end
                if len(raw_seq) != 8 or len(raw_len) != 4:
                    self.corrupt = True
                    return
                (seq,) = struct.unpack(">Q", raw_seq)
                (enc_len,) = struct.unpack(">I", raw_len)
                if enc_len < 13 or enc_len > (MAX_EVENT_BYTES + 64):
                    self.corrupt = True
                    return
                if seq <= after:
                    if handle.tell() + enc_len > file_size:
                        self.corrupt = True
                        return
                    handle.seek(enc_len, os.SEEK_CUR)
                    continue
                blob = handle.read(enc_len)
                if len(blob) != enc_len:
                    self.corrupt = True
                    return
                try:
                    plain = self.box.decrypt(
                        blob,
                        associated_data=_frame_ad(version, self.session_id,
                                                  seq))
                except Exception:
                    self.corrupt = True
                    return
                yield seq, plain


class LocalSpool:
    """Bounded encrypted append-only spool for one session.

    Layout under ``root`` (deterministic, bounded)::

        root/segments/<session_id>_<NNNNNN>.active    # being written
        root/segments/<session_id>_<NNNNNN>.sealed    # sealed atomically
        root/checkpoints/<session_id>.json            # durable cursor

    The active segment is created lazily on the first write, and a segment
    that never received a frame is deleted (not sealed) on close, so idle
    open/close cycles leave no files behind.

    ``quota_root`` is the directory whose spools share the machine byte
    quota (default ``root``).  Spool roots laid out as
    ``quota_root/<session>/segments`` are all counted, and admission is
    serialized on ``quota_root``.  ``owner_lock=True`` holds the per-root
    writer lock (:func:`acquire_owner_lock`, waiting at most
    ``owner_timeout`` seconds, else :class:`SpoolBusy`) until :meth:`close`.
    """

    def __init__(
        self,
        root,
        machine_id: str,
        session_id: str,
        max_session_bytes: int = 33554432,
        max_machine_bytes: int = 134217728,
        key: bytes | None = None,
        segment_target_bytes: int = _SEGMENT_TARGET,
        quota_root=None,
        owner_lock: bool = False,
        owner_timeout: float = OWNER_WAIT_S,
    ):
        self._root = Path(root)
        self._quota_root = Path(quota_root) if quota_root is not None else self._root
        self._machine_id = machine_id
        self._session_id = session_id
        self._max_session_bytes = int(max_session_bytes)
        self._max_machine_bytes = int(max_machine_bytes)
        self._segment_target = int(segment_target_bytes)

        # Fail closed when no key is available: encryption is never skipped.
        if key is None or len(key) != 32:
            raise SpoolError(CODE_KEY_MISSING)
        self._box = AeadBox(key)

        self._seg_dir = self._root / "segments"
        self._cp_dir = self._root / "checkpoints"
        try:
            self._seg_dir.mkdir(parents=True, exist_ok=True)
            self._cp_dir.mkdir(parents=True, exist_ok=True)
            self._quota_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SpoolError(CODE_SETUP) from exc

        self._checkpoint_path = self._cp_dir / f"{session_id}.json"
        self._seg_re = _segment_pattern(session_id)
        self._open_writer = None
        self._writer_version = _MAGIC
        self._seg_index = 0
        self._last_sequence = 0
        self._ack_sequence = 0
        self._gap_signaled = False
        # A checkpoint write that failed AFTER its frame was durable: the
        # frame is the durable truth (``_load`` recovers it), so the cursor is
        # only marked dirty and re-persisted on the next operation/close.
        self._checkpoint_dirty = False

        self._owner_handle = None
        if owner_lock:
            self._owner_handle = acquire_owner_lock(
                self._root, timeout=owner_timeout)
        try:
            self._load()
        except BaseException:
            release_owner_lock(self._owner_handle)
            self._owner_handle = None
            raise

    # -- public surface -----------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def machine_id(self) -> str:
        return self._machine_id

    def append(self, event) -> AppendResult:
        """Validate, sequence and durably persist one event.

        Returns :class:`AppendResult`; raises :class:`ValueError` for a
        non-validated event (bounded error, no content leak).  Never
        silently discards an exact/structured event and never writes a
        partial record.
        """
        clean = validate_event(event)  # raises ValueError on invalid
        with _quota_guard(self._quota_root):
            self._retry_dirty_checkpoint()
            seq = self._next_sequence()
            # The spool owns sequence assignment: the caller-supplied value
            # (a best-effort redaction hint) never shapes the durable record.
            # The persisted body carries exactly the assigned sequence so
            # replay ordering and the ack cursor stay consistent.
            clean["sequence"] = seq
            body = json.dumps(clean, ensure_ascii=True,
                              separators=(",", ":")).encode("utf-8")
            if not self._within_quota(_frame_footprint(len(body))):
                return self._handle_overflow(clean, seq)

            self._write_frame(seq, body)
            self._last_sequence = seq
            self._gap_signaled = False  # a new accepted event ends the gap run
            self._maybe_rotate()
            # The frame is durable and the sequence assigned: a checkpoint
            # failure here must not report the event as lost.
            self._persist_checkpoint_soft()
            return AppendResult(sequence=seq, capture_blocked=False)

    def read_after(self, sequence: int, limit: int, max_bytes: int) -> list:
        """Unacknowledged records strictly after ``sequence``, bounded by
        ``limit`` events and ``max_bytes`` (never unbounded)."""
        try:
            base = max(int(sequence), self._ack_sequence)
            limit = int(limit)
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            raise ValueError("read_after bounds must be integers") from None
        if limit <= 0 or max_bytes <= 0:
            return []
        out: list = []
        budget = max_bytes
        for seq, plain in self._scan_all(after=base):
            if seq <= base:
                continue
            try:
                event = json.loads(plain.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            size = len(json.dumps(event, ensure_ascii=True,
                                  separators=(",", ":")))
            if size > budget:
                break
            out.append(event)
            budget -= size
            if len(out) >= limit:
                break
        out.sort(key=lambda e: e.get("sequence", 0))
        return out

    def replay_from(self) -> list:
        """Replay every unacknowledged record (bounded convenience alias)."""
        return self.read_after(self._ack_sequence, 1 << 20, 1 << 28)

    def ack(self, sequence: int) -> None:
        """Durable ack: record the ack cursor and compact acked segments.

        Replay starts strictly after the ack cursor, so acknowledged records
        are never re-sent.  Decreasing/de-duplicate ack is a no-op.
        """
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            return
        # ack beyond the last durable sequence is clamped: there are no
        # records out there to acknowledge.
        sequence = min(sequence, self._last_sequence)
        if sequence <= self._ack_sequence:
            return
        self._ack_sequence = sequence
        self._persist_checkpoint()
        self._compact_acked()

    def rotate(self) -> None:
        """Seal the active segment atomically and open a fresh segment."""
        with _quota_guard(self._quota_root):
            self._rotate_unlocked()

    def checkpoint(self) -> dict:
        """Persist state durably and return a bounded status snapshot."""
        self._persist_checkpoint()
        return self.status()

    def status(self) -> dict:
        """Bounded status — never any paths, exception text or payloads."""
        return {
            "machine_id": self._machine_id,
            "session_id": self._session_id,
            "last_sequence": self._last_sequence,
            "next_sequence": self._next_sequence(),
            "ack_sequence": self._ack_sequence,
            "session_bytes": self._session_bytes(),
            "machine_bytes": self._machine_bytes(),
            "session_quota": self._max_session_bytes,
            "machine_quota": self._max_machine_bytes,
            "segment_count": self._segment_count(),
            "gap_signaled": self._gap_signaled,
        }

    def close(self) -> None:
        """Flush, seal the active segment and close file handles.

        Idempotent; releases the owner lock (if held) last.
        """
        try:
            self._retry_dirty_checkpoint()
            self._seal_active()
        finally:
            self._open_writer = None
            handle, self._owner_handle = self._owner_handle, None
            release_owner_lock(handle)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _next_sequence(self) -> int:
        return self._last_sequence + 1

    def _load(self) -> None:
        if self._checkpoint_path.exists():
            try:
                cp = json.loads(self._checkpoint_path.read_text("utf-8"))
                self._last_sequence = int(cp.get("last_sequence", 0))
                self._ack_sequence = int(cp.get("ack_sequence", 0))
                self._gap_signaled = bool(cp.get("gap_signaled", False))
            except Exception:
                # A corrupt checkpoint must not leak; restart at sequence 1
                # but keep any intact encrypted segments (readers skip them).
                self._last_sequence = 0
                self._ack_sequence = 0
                self._gap_signaled = False
        # The durable truth is the largest COMPLETE frame on disk.  A torn
        # frame's sequence is never counted durable (which would open a
        # permanent monotonic gap on the next append), and a durable frame is
        # never accidentally re-assigned.  When the checkpoint records a
        # frame whose body later arrived only partway (crash/truncate), the
        # recovered max bounds BOTH the resume sequence and the ack cursor so
        # the invariant ``ack <= last_sequence`` holds after a restart.
        recovered = self._scan_max_sequence()
        if recovered > 0:
            self._last_sequence = recovered
            if self._ack_sequence > recovered:
                self._ack_sequence = recovered
        # Segment continuity after a restart: if the highest-index segment is
        # already sealed (clean close) a FRESH segment must be opened for new
        # appends, otherwise the new "active" would share an index with a
        # sealed file.  If the highest is still an "active" it is a crash
        # survivor: truncate any torn trailing frame back to the last complete
        # frame boundary BEFORE appending, so replay never mis-frames later
        # bytes.
        self._drop_empty_sealed()
        self._seg_index = self._max_seg_index()
        current = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}"
        if current.with_suffix(".sealed").exists():
            self._seg_index += 1
        else:
            self._repair_torn_tail()
        # The active segment itself is opened lazily on the first write.

    def _within_quota(self, frame_cost: int) -> bool:
        if self._open_writer is None:
            # The lazily created active segment's header is part of the cost.
            active = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}.active"
            if not active.exists():
                frame_cost += _HEADER_SIZE
        if self._session_bytes() + frame_cost > self._max_session_bytes:
            return False
        if self._machine_bytes() + frame_cost > self._max_machine_bytes:
            return False
        return True

    def _handle_overflow(self, clean: dict, seq: int) -> AppendResult:
        quality = clean.get("capture_quality")
        if quality == "best_effort":
            return self._record_gap(clean, seq)
        # exact / structured: explicit pause signal, never a silent drop.
        return AppendResult(sequence=None, capture_blocked=True)

    def _record_gap(self, dropped: dict, seq: int) -> AppendResult:
        """Best-effort overflow: persist (or signal) one capture_gap.

        The tiny ``capture_gap`` marker is persisted when the quota allows
        (it is small, so usually fits even in a full spool); in the
        pathological case where even the marker does not fit, the in-memory
        ``gap_sequence`` signal is still returned.  Never reported as an
        accepted event slot.
        """
        if self._gap_signaled:
            # Already in a gap run; do not flood the stream with markers.
            return AppendResult(sequence=None, capture_blocked=False,
                                gap_sequence=seq)
        gap_evt = _build_gap_event(dropped, seq)
        body = json.dumps(gap_evt, ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8")
        if self._within_quota(_frame_footprint(len(body))):
            try:
                self._write_frame(seq, body)
            except SpoolError:
                # 标记写入失败：仍然发出内存中的 gap 信号，不推进序号。
                pass
            else:
                self._last_sequence = seq
                self._persist_checkpoint_soft()
        # The gap is always *signaled*, even if only the in-memory marker
        # could be produced (the quota is exhausted).
        self._gap_signaled = True
        return AppendResult(sequence=None, capture_blocked=False,
                            gap_sequence=seq)

    # -- filesystem ----------------------------------------------------------

    def _segment_paths(self) -> list:
        try:
            names = os.listdir(self._seg_dir)
        except OSError:
            return []
        return [self._seg_dir / name for name in sorted(names)
                if self._seg_re.match(name)]

    def _segment_count(self) -> int:
        return len(self._segment_paths())

    def _seg_index_of(self, path: Path) -> int:
        match = self._seg_re.match(path.name)
        return int(match.group(1)) if match else 0

    def _max_seg_index(self) -> int:
        biggest = 0
        for path in self._segment_paths():
            biggest = max(biggest, self._seg_index_of(path))
        return biggest

    def _drop_empty_sealed(self) -> None:
        """Delete header-only sealed segments left by older builds (every
        idle open/close used to seal one); they hold no frame."""
        for path in self._segment_paths():
            if path.suffix != ".sealed":
                continue
            try:
                if path.stat().st_size <= _HEADER_SIZE:
                    path.unlink()
            except OSError:
                pass

    def _last_complete_frame_end(self, path: Path) -> int:
        """Byte offset just past the last COMPLETE frame in ``path``.

        A torn trailing frame (bad header, bad length, short body) is left
        for the caller to truncate; this returns ``-1`` when no complete
        frame exists in the file.
        """
        if not path.exists():
            return -1
        try:
            handle = open(path, "rb")
        except OSError:
            return 0
        with handle:
            head = handle.read(_HEADER_SIZE)
            if head[: len(_MAGIC)] not in _MAGICS or len(head) != _HEADER_SIZE:
                return 0
            offset = _HEADER_SIZE
            last_good = 0
            while True:
                raw_seq = handle.read(8)
                raw_len = handle.read(4)
                if len(raw_seq) == 0 and len(raw_len) == 0:
                    return offset  # clean EOF at a frame boundary
                if len(raw_seq) != 8 or len(raw_len) != 4:
                    return last_good  # torn frame header tail
                (enc_len,) = struct.unpack(">I", raw_len)
                if enc_len < 13 or enc_len > (MAX_EVENT_BYTES + 64):
                    return last_good  # invalid length -> torn
                blob = handle.read(enc_len)
                if len(blob) != enc_len:
                    return last_good  # torn frame body
                last_good = offset + _FRAME_HEADER + enc_len
                offset = last_good

    def _repair_torn_tail(self) -> None:
        """Truncate the current segment's torn trailing bytes so appends
        written after a crash are readable (crash-injection recovery)."""
        path = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}.active"
        if not path.exists():
            return
        end = self._last_complete_frame_end(path)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if 0 <= end < size:
            try:
                with open(path, "r+b") as fh:
                    fh.truncate(end)
            except OSError:
                pass

    def _scan_max_sequence(self) -> int:
        """Largest sequence in COMPLETE segment frames (crash-injection safe).

        A torn/corrupt frame is skipped — its sequence never advances the
        recovery cursor (a partial write is not durable).
        """
        best = 0
        for path in self._segment_paths():
            best = max(best, self._frame_max_seq(path))
        return best

    @staticmethod
    def _frame_max_seq(path: Path) -> int:
        best = 0
        try:
            with open(path, "rb") as handle:
                head = handle.read(_HEADER_SIZE)
                if head[: len(_MAGIC)] not in _MAGICS or len(head) != _HEADER_SIZE:
                    return 0
                while True:
                    raw_seq = handle.read(8)
                    raw_len = handle.read(4)
                    if len(raw_seq) == 0 and len(raw_len) == 0:
                        break
                    if len(raw_seq) != 8 or len(raw_len) != 4:
                        break
                    (seq,) = struct.unpack(">Q", raw_seq)
                    (enc_len,) = struct.unpack(">I", raw_len)
                    if enc_len < 13 or enc_len > (MAX_EVENT_BYTES + 64):
                        break
                    # The frame's seq is only durable when its BODY is fully
                    # on disk: a torn/partial body (crash-injection) must NOT
                    # credit its header seq to the recovery cursor, else a
                    # reopen would silently skip that sequence (a permanent
                    # monotonic gap with no capture_gap marker).
                    blob = handle.read(enc_len)
                    if len(blob) != enc_len:
                        break
                    best = max(best, seq)
        except OSError:
            return 0
        return best

    def _scan_all(self, after: int = 0):
        """Yield ``(seq, plaintext)`` for every complete frame with
        ``seq > after``, in order.

        已确认前缀不再逐批解密：sealed 段不可变，缓存其最大序号，整段都
        ``<= after`` 时直接跳过；段内已确认帧只读帧头并 seek 过密文。
        """
        cache = self.__dict__.setdefault("_sealed_max_seq", {})
        for path in self._segment_paths():
            if after > 0 and path.suffix == ".sealed":
                try:
                    st = path.stat()
                    key = (path.name, st.st_size, st.st_mtime_ns)
                except OSError:
                    key = None
                if key is not None and key not in cache:
                    cache[key] = self._frame_max_seq(path)
                if key is not None and cache[key] <= after:
                    continue
            reader = _FrameReader(path, self._box, self._session_id)
            for seq, plain in reader.frames(after=after):
                yield seq, plain

    def _size(self) -> int:
        try:
            return sum(p.stat().st_size for p in self._segment_paths())
        except OSError:
            return 0

    def _session_bytes(self) -> int:
        return self._size()

    def _machine_bytes(self) -> int:
        """Segment bytes of every spool sharing ``quota_root``.

        Counts ``quota_root/segments`` (sessions sharing one root) and each
        ``quota_root/<session>/segments`` (one root per session, the runner
        capture layout), plus this spool's own directory when it lives
        elsewhere.
        """
        dirs = {self._seg_dir}
        try:
            for child in os.scandir(self._quota_root):
                if child.name == "segments":
                    dirs.add(Path(child.path))
                elif child.is_dir(follow_symlinks=False):
                    seg = Path(child.path) / "segments"
                    if seg.is_dir():
                        dirs.add(seg)
        except OSError:
            pass
        total = 0
        for seg_dir in dirs:
            try:
                for entry in os.scandir(seg_dir):
                    if self._owns_segment(Path(entry.name)):
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    @staticmethod
    def _owns_segment(path: Path) -> bool:
        name = path.name
        return name.endswith(".sealed") or name.endswith(".active")

    def _open_active(self) -> None:
        if self._open_writer is not None:
            return
        path = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}.active"
        try:
            handle = open(path, "ab")
        except OSError as exc:
            raise SpoolError(CODE_SETUP) from exc
        version = _MAGIC
        if handle.tell() == 0:
            handle.write(_MAGIC)
            handle.write(struct.pack(">Q", 0))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        else:
            # A crash-survivor active segment keeps its on-disk format.
            try:
                with open(path, "rb") as head:
                    magic = head.read(len(_MAGIC))
            except OSError:
                magic = _MAGIC
            if magic == _MAGIC_V1:
                version = _MAGIC_V1
        self._open_writer = handle
        self._writer_version = version
        self._active_path = path

    def _write_frame(self, seq: int, body: bytes) -> None:
        """Durably write one frame or raise :class:`SpoolError`.

        写入/flush/fsync 任一步失败（如 ENOSPC）都不能把序号交给调用方：
        截断回写入前位置并抛 ``spool_write_failed``，调用方不推进
        ``_last_sequence``/checkpoint。半截帧若无法截断，则换新段，避免它挡住
        后续帧（读取器在坏帧处停止的是本段，不影响其它段）。
        """
        self._open_active()
        blob = self._box.encrypt(
            body, associated_data=_frame_ad(self._writer_version,
                                            self._session_id, seq))
        frame = struct.pack(">QI", seq, len(blob)) + blob
        writer = self._open_writer
        try:
            start = writer.tell()
        except OSError as exc:
            raise SpoolError(CODE_WRITE) from exc
        try:
            writer.write(frame)
            writer.flush()
            os.fsync(writer.fileno())
        except OSError as exc:
            self._discard_partial_frame(writer, start)
            raise SpoolError(CODE_WRITE) from exc

    def _discard_partial_frame(self, writer, start: int) -> None:
        """Drop the writer and cut the active segment back to ``start``."""
        self._open_writer = None
        path = getattr(self, "_active_path", None)
        try:
            # close() 可能再次尝试 flush 缓冲区；失败也必须继续截断。
            writer.close()
        except OSError:
            pass
        try:
            if path is None:
                raise OSError("no active segment")
            os.truncate(path, start)
        except OSError:
            # 无法截断：后续帧写到新段，坏尾只影响当前段的回放。
            self._seg_index += 1

    def _maybe_rotate(self) -> None:
        try:
            current = self._open_writer.tell()
        except (OSError, AttributeError):
            current = 0
        if current >= self._segment_target:
            self._rotate_unlocked()

    def _rotate_unlocked(self) -> None:
        """Rotate while the caller already owns the quota guard.

        The next segment is created lazily by the next write.
        """
        self._seal_active()
        self._seg_index += 1

    def _seal_active(self) -> None:
        """Seal the current active segment (also a crash survivor that was
        never reopened for writing); a frame-less one is deleted instead."""
        w = self._open_writer
        if w is not None:
            try:
                w.flush()
                os.fsync(w.fileno())
            except OSError:
                pass
            finally:
                try:
                    w.close()
                except OSError:
                    pass
            self._open_writer = None
        active = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}.active"
        sealed = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}.sealed"
        try:
            if active.stat().st_size <= _HEADER_SIZE:
                active.unlink()
                return
        except OSError:
            return
        try:
            active.replace(sealed)
        except OSError:
            pass

    def _compact_acked(self) -> None:
        """Delete fully-acked sealed segments (idempotent, isolated)."""
        for path in self._segment_paths():
            if path.suffix != ".sealed":
                continue
            try:
                if self._frame_max_seq(path) <= self._ack_sequence:
                    path.unlink()
            except OSError:
                pass

    def _persist_checkpoint(self) -> None:
        """Durable cursor write: temp + fsync + atomic rename + dir fsync."""
        payload = {
            "schema_version": 1,
            "last_sequence": self._last_sequence,
            "ack_sequence": self._ack_sequence,
            "gap_signaled": self._gap_signaled,
        }
        tmp = self._checkpoint_path.with_name(
            f"{self._session_id}.json.tmp-{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=True, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._checkpoint_path)
        except OSError:
            self._checkpoint_dirty = True
            raise SpoolError(CODE_CHECKPOINT_BAD) from None
        self._checkpoint_dirty = False
        self._fsync_dir(self._cp_dir)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """Best-effort directory fsync (rename durability on POSIX).

        Windows cannot open a directory with ``os.open`` (and does not need
        it: ``os.replace`` is durable there), so it is skipped; any other
        failure is ignored — the file contents are already fsynced.
        """
        if _IS_WINDOWS:
            return
        try:
            dfd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dfd)
        except OSError:
            pass
        finally:
            os.close(dfd)

    def _persist_checkpoint_soft(self) -> None:
        """Persist after a durable frame; a failure only marks it dirty."""
        try:
            self._persist_checkpoint()
        except SpoolError:
            self._checkpoint_dirty = True

    def _retry_dirty_checkpoint(self) -> None:
        if self._checkpoint_dirty:
            self._persist_checkpoint_soft()


__all__ = [
    "AppendResult",
    "LocalSpool",
    "SpoolBusy",
    "SpoolError",
    "MAX_EVENT_BYTES",
    "acquire_owner_lock",
    "owner_lock_held",
    "release_owner_lock",
]
