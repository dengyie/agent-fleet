"""Bounded ACP JSON-RPC 2.0 stdio transport (one JSON object per UTF-8 line)."""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from collections.abc import Mapping

MAX_FRAME = 1 << 20
MAX_PENDING = 128


class AcpTransportError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _InvalidJson(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson('duplicate_key')
        result[key] = value
    return result


def _constant(_value):
    raise _InvalidJson('non_finite_number')


class AcpTransport:
    def __init__(self, process, *, on_notification=None, on_client_request=None,
                 should_abort=None):
        self.process = process
        self.on_notification = on_notification or (lambda _row: None)
        self.on_client_request = on_client_request or (lambda _row: {'error': {'code': -32601, 'message': 'Method not found'}})
        self.should_abort = should_abort or (lambda: False)
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._stopped = threading.Event()
        self._state_lock = threading.Lock()
        self._error = None
        self._client_slots = threading.BoundedSemaphore(16)
        self._client_inflight = 0
        self._client_lock = threading.Lock()
        self._client_ids = set()
        self._outgoing = queue.Queue(maxsize=1)
        self._writer = threading.Thread(target=self._write_loop, name='acp-stdin', daemon=True)
        self._reader = threading.Thread(target=self._read_loop, name='acp-stdout', daemon=True)
        self._stderr = threading.Thread(target=self._stderr_loop, name='acp-stderr', daemon=True)
        self._reader.start()
        self._stderr.start()
        self._writer.start()

    @property
    def error(self): return self._error

    @property
    def client_inflight(self):
        with self._client_lock: return self._client_inflight

    def _write_loop(self):
        while not self._stopped.is_set():
            try: raw, done = self._outgoing.get(timeout=.05)
            except queue.Empty: continue
            try:
                stream = self.process.stdin
                view = memoryview(raw)
                while view:
                    wrote = stream.write(view)
                    if not wrote: raise OSError('closed')
                    view = view[wrote:]
                stream.flush()
            except (OSError, ValueError): self._fail('protocol_eof')
            finally: done.set()

    def _stderr_loop(self):
        stream = getattr(self.process, 'stderr', None)
        if stream is None: return
        try:
            while not self._stopped.is_set():
                chunk = stream.read(4096)
                if not chunk: return
        except (OSError, ValueError):
            return

    def _frames(self, stream):
        # FileIO.readline() on the owned unbuffered pipe reads one byte per
        # syscall. read1() also avoids filling a buffered stream before it
        # returns. Decode only complete frames, preserving split UTF-8 bytes.
        read = getattr(stream, 'read1', None) or stream.read
        pending = bytearray()
        while not self._stopped.is_set():
            chunk = read(65536)
            if not chunk:
                raise AcpTransportError('frame_too_large' if pending else 'protocol_eof')
            start = 0
            while start < len(chunk):
                newline = chunk.find(b'\n', start)
                end = newline + 1 if newline >= 0 else len(chunk)
                if len(pending) + end - start > MAX_FRAME:
                    raise AcpTransportError('frame_too_large')
                pending.extend(chunk[start:end])
                if newline >= 0:
                    yield bytes(pending)
                    pending.clear()
                    if self._stopped.is_set():
                        return
                start = end

    def _read_loop(self):
        stream = getattr(self.process, 'stdout', None)
        if stream is None:
            self._fail('protocol_eof'); return
        try:
            for raw in self._frames(stream):
                try:
                    row = json.loads(raw.decode('utf-8', errors='strict'),
                        object_pairs_hook=_object, parse_constant=_constant)
                except (UnicodeDecodeError, ValueError, RecursionError):
                    self._fail('invalid_frame'); return
                if not isinstance(row, Mapping) or row.get('jsonrpc') != '2.0':
                    self._fail('invalid_frame'); return
                if 'method' in row:
                    if 'result' in row or 'error' in row:
                        self._fail('invalid_frame'); return
                    if not isinstance(row.get('method'), str) or len(row['method']) > 128:
                        self._fail('invalid_frame'); return
                    if 'id' in row:
                        rid = row['id']
                        if (type(rid) not in (str, int) or isinstance(rid, str) and len(rid) > 256
                                or rid in self._client_ids or len(self._client_ids) >= 256
                                or not self._client_slots.acquire(blocking=False)):
                            self._fail('client_request_limit'); return
                        self._client_ids.add(rid)
                        with self._client_lock: self._client_inflight += 1
                        threading.Thread(target=self._answer_client_request, args=(row,), daemon=True).start()
                    else:
                        try: self.on_notification(row)
                        except AcpTransportError: raise
                        except Exception:
                            self._fail('invalid_update'); return
                    continue
                request_id = row.get('id')
                if (type(request_id) is not int or ('result' in row) == ('error' in row)
                        or set(row) - {'jsonrpc', 'id', 'result', 'error'}):
                    self._fail('invalid_response'); return
                if 'error' in row:
                    error = row['error']
                    if (not isinstance(error, Mapping) or type(error.get('code')) is not int
                            or not isinstance(error.get('message'), str) or len(error['message']) > 1024):
                        self._fail('invalid_response'); return
                elif not isinstance(row.get('result'), Mapping):
                    self._fail('invalid_response'); return
                with self._pending_lock: waiter = self._pending.get(request_id)
                if waiter is None:
                    self._fail('unexpected_response'); return
                try: waiter.put_nowait(row)
                except queue.Full:
                    self._fail('duplicate_response'); return
        except AcpTransportError as exc:
            self._fail(exc.code)
        except (OSError, ValueError, RecursionError):
            self._fail('protocol_eof')

    def _answer_client_request(self, row):
        try:
            outcome = self.on_client_request(row)
            if not isinstance(outcome, Mapping) or ('result' in outcome) == ('error' in outcome):
                raise AcpTransportError('client_request_failed')
            callback = outcome.get('_on_delivered')
            outcome = {k: outcome[k] for k in ('result', 'error') if k in outcome}
            self._send({'jsonrpc': '2.0', 'id': row['id'], **dict(outcome)})
            if callable(callback): callback()
        except AcpTransportError as exc: self._fail(exc.code)
        except Exception: self._fail('client_request_failed')
        finally:
            with self._client_lock: self._client_inflight -= 1
            self._client_slots.release()

    def _fail(self, code):
        with self._state_lock:
            if self._error is None: self._error = code
            code = self._error
            self._stopped.set()
        with self._pending_lock: pending = list(self._pending.values())
        for waiter in pending:
            try: waiter.put_nowait({'_fleet_error': code})
            except queue.Full: pass

    def _send(self, row):
        deadline = time.monotonic() + 1
        is_request = 'id' in row and 'method' in row
        if is_request and self.should_abort(): raise AcpTransportError('cancelled')
        raw = (json.dumps(row, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if len(raw) > MAX_FRAME: raise AcpTransportError('frame_too_large')
        while True:
            if self._error: raise AcpTransportError(self._error)
            if is_request and self.should_abort(): raise AcpTransportError('cancelled')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail('write_timeout')
                raise AcpTransportError(self._error)
            if self._write_lock.acquire(timeout=min(.05, remaining)): break
        try:
            if getattr(self.process, 'stdin', None) is None or self._stopped.is_set(): raise AcpTransportError(self._error or 'protocol_eof')
            if is_request and self.should_abort(): raise AcpTransportError('cancelled')
            done = threading.Event()
            try: self._outgoing.put((raw, done), timeout=max(0, deadline - time.monotonic()))
            except queue.Full as exc:
                self._fail('write_timeout')
                raise AcpTransportError(self._error) from exc
            while not done.wait(min(.05, max(0, deadline - time.monotonic()))):
                if self._error: raise AcpTransportError(self._error)
                if time.monotonic() >= deadline:
                    self._fail('write_timeout')
                    raise AcpTransportError(self._error)
            if self._error: raise AcpTransportError(self._error)
        finally:
            self._write_lock.release()

    def notify(self, method, params): self._send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def request(self, method, params, *, timeout=90):
        with self._pending_lock:
            if len(self._pending) >= MAX_PENDING: raise AcpTransportError('pending_limit')
            request_id, waiter = next(self._ids), queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            deadline = time.monotonic() + max(.01, timeout)
            while True:
                if self.should_abort(): raise AcpTransportError('cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise AcpTransportError('request_timeout')
                try: response = waiter.get(timeout=min(.05, remaining)); break
                except queue.Empty:
                    if self._error: raise AcpTransportError(self._error)
            if '_fleet_error' in response: raise AcpTransportError(response['_fleet_error'])
            if response.get('jsonrpc') != '2.0': raise AcpTransportError('invalid_response')
            if response.get('error') is not None: raise AcpTransportError('provider_error')
            result = response.get('result')
            if not isinstance(result, Mapping): raise AcpTransportError('invalid_response')
            return dict(result)
        finally:
            with self._pending_lock: self._pending.pop(request_id, None)

    def close(self): self._fail('protocol_eof')
