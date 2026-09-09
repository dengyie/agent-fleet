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
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from session_schema import validate_event
from tools.session.crypto import AeadBox

# Stable diagnostic codes (never raw paths / exception text / payloads).
CODE_SEGMENT_BAD = "segment_corrupt"
CODE_CHECKPOINT_BAD = "checkpoint_corrupt"
CODE_KEY_MISSING = "spool_key_missing"
CODE_SETUP = "spool_setup"

_MAGIC = b"SP4SEG01"
_HEADER_SIZE = len(_MAGIC) + 8  # magic + reserved u64
_FRAME_HEADER = 12              # u64 seq + u32 enc_len
_SEGMENT_TARGET = 262144        # 256 KiB per segment when full
MAX_EVENT_BYTES = 65536         # shared single-event bound (post-redaction)

# Global AEAD associated data: binds every segment frame to this spool format.
_AD = b"fleet-spool-v1"


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


class _FrameReader:
    """Incremental frame reader over ONE encrypted segment file.

    ``frames()`` yields ``(seq, plaintext)`` per complete, authenticated
    frame.  On a checksum/tag failure or truncated frame it stops playback
    for that segment (``corrupt=True``) — a corrupt segment never poisons the
    rest of the spool stream.
    """

    def __init__(self, path: Path, box: AeadBox):
        self.path = path
        self.box = box
        self.corrupt = False

    def frames(self):
        try:
            handle = open(self.path, "rb")
        except OSError:
            self.corrupt = True
            return
        with handle:
            head = handle.read(_HEADER_SIZE)
            if head[: len(_MAGIC)] != _MAGIC or len(head) != _HEADER_SIZE:
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
                blob = handle.read(enc_len)
                if len(blob) != enc_len:
                    self.corrupt = True
                    return
                try:
                    plain = self.box.decrypt(
                        blob,
                        associated_data=str(seq).encode("ascii"))
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
    ):
        self._root = Path(root)
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
        except OSError as exc:
            raise SpoolError(CODE_SETUP) from exc

        self._checkpoint_path = self._cp_dir / f"{session_id}.json"
        self._open_writer = None
        self._seg_index = 0
        self._last_sequence = 0
        self._ack_sequence = 0
        self._gap_signaled = False

        self._load()

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
        seq = self._next_sequence()
        # The spool owns sequence assignment: the caller-supplied value (a
        # best-effort redaction hint) never shapes the durable record.  The
        # persisted body carries exactly the assigned sequence so replay
        # ordering and the ack cursor stay consistent.
        clean["sequence"] = seq
        body = json.dumps(clean, ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8")
        if not self._within_quota(_frame_footprint(len(body))):
            return self._handle_overflow(clean, seq)

        self._write_frame(seq, body)
        self._last_sequence = seq
        self._gap_signaled = False  # a new accepted event ends the gap run
        self._maybe_rotate()
        self._persist_checkpoint()
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
        for seq, plain in self._scan_all():
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
        self._seal_active()
        self._seg_index += 1
        self._open_active()

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
        """Flush, seal the active segment and close file handles."""
        try:
            self._seal_active()
        finally:
            self._open_writer = None

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
        self._seg_index = self._max_seg_index()
        # Segment continuity after a restart: if the highest-index segment is
        # already sealed (clean close) a FRESH segment must be opened for new
        # appends, otherwise the new "active" would share an index with a
        # sealed file.  If the highest is still an "active" it is a crash
        # survivor: truncate any torn trailing frame back to the last complete
        # frame boundary BEFORE appending, so replay never mis-frames later
        # bytes.
        current = self._seg_dir / f"{self._session_id}_{self._seg_index:06d}"
        if current.with_suffix(".sealed").exists():
            self._seg_index += 1
        else:
            self._repair_torn_tail()
        self._open_active()

    def _within_quota(self, frame_cost: int) -> bool:
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
            self._write_frame(seq, body)
            self._last_sequence = seq
            self._persist_checkpoint()
        # The gap is always *signaled*, even if only the in-memory marker
        # could be produced (the quota is exhausted).
        self._gap_signaled = True
        return AppendResult(sequence=None, capture_blocked=False,
                            gap_sequence=seq)

    # -- filesystem ----------------------------------------------------------

    def _segment_paths(self) -> list:
        return sorted(self._seg_dir.glob(f"{self._session_id}_*"),
                      key=lambda p: p.name)

    def _segment_count(self) -> int:
        return len(self._segment_paths())

    def _seg_index_of(self, path: Path) -> int:
        try:
            digits = path.name.rsplit("_", 1)[1].split(".", 1)[0]
            return int(digits)
        except (IndexError, ValueError):
            return 0

    def _max_seg_index(self) -> int:
        biggest = 0
        for path in self._seg_dir.glob(f"{self._session_id}_*"):
            biggest = max(biggest, self._seg_index_of(path))
        return biggest

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
            if head[: len(_MAGIC)] != _MAGIC or len(head) != _HEADER_SIZE:
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
                if head[: len(_MAGIC)] != _MAGIC or len(head) != _HEADER_SIZE:
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

    def _scan_all(self):
        """Yield ``(seq, plaintext)`` for every complete frame, in order."""
        for path in self._segment_paths():
            reader = _FrameReader(path, self._box)
            for seq, plain in reader.frames():
                yield seq, plain

    def _size(self) -> int:
        try:
            return sum(p.stat().st_size for p in self._segment_paths())
        except OSError:
            return 0

    def _session_bytes(self) -> int:
        return self._size()

    def _machine_bytes(self) -> int:
        try:
            total = 0
            for p in self._seg_dir.glob("*"):
                if p.is_file() and self._owns_segment(p):
                    total += p.stat().st_size
            return total
        except OSError:
            return 0

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
        if handle.tell() == 0:
            handle.write(_MAGIC)
            handle.write(struct.pack(">Q", 0))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        self._open_writer = handle

    def _write_frame(self, seq: int, body: bytes) -> None:
        self._open_active()
        blob = self._box.encrypt(
            body, associated_data=str(seq).encode("utf-8"))
        frame = struct.pack(">QI", seq, len(blob)) + blob
        self._open_writer.write(frame)
        try:
            self._open_writer.flush()
            os.fsync(self._open_writer.fileno())
        except OSError:
            pass

    def _maybe_rotate(self) -> None:
        try:
            current = self._open_writer.tell()
        except (OSError, AttributeError):
            current = 0
        if current >= self._segment_target:
            self.rotate()

    def _seal_active(self) -> None:
        w = self._open_writer
        if w is None:
            return
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
            active.replace(sealed)
        except OSError:
            pass

    def _compact_acked(self) -> None:
        """Delete fully-acked sealed segments (idempotent, isolated)."""
        for path in self._seg_dir.glob(f"{self._session_id}_*.sealed"):
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
            dfd = os.open(self._cp_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            raise SpoolError(CODE_CHECKPOINT_BAD) from None


__all__ = [
    "AppendResult",
    "LocalSpool",
    "SpoolError",
    "MAX_EVENT_BYTES",
]