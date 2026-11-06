"""Task 12 — end-to-end acceptance: adopted receipt promotion + observe, detach
without signal, drift auto-revoke (``adoption_drift`` audit + zero signals),
and the disabled-flag surface.

The four golden tests are VERBATIM from the task brief.  The harness assembles
REAL pieces: a real hub app (``create_app`` on a fully-enabled ``FleetConfig``),
a real ``ControlClient`` talking to it through the app's own HTTP routes (an
in-process transport adapter over ``app.post``), and a REAL
``tools.supervisor.supervisor.Supervisor`` probe whose identity reader is
driven by a mutable in-memory ``process`` double:

* ``e2e.operator_adopt()``         -> POST /api/adoptions (operator) + repo row;
* ``e2e.probe_poll_and_receipt()`` -> one real ``ControlClient.poll_once()``
  (the client uploads its bounded receipt through the same transport);
* ``e2e.adopted_session()``        -> operator adopt + probe promote;
* ``e2e.operator_delete()``        -> DELETE /api/adoptions/<session_id>;
* ``e2e.operator_control()``       -> POST /api/adoption/<sid>/source/control;
* ``e2e.ingest_native_line()``     -> REAL session ingest pipeline;
* ``e2e.read_redacted()``          -> REAL transcript repo read;
* ``e2e.audit()``                  -> REAL adoption transcript ``read_audit()``.

The process and all ops are record-only: any signal the probe ever attempts is
observable on ``e2e.process.signals`` — the drift/revoke paths assert it stays
empty.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hub.bootstrap import create_app
from support.hub_env import HubStores
from tools.supervisor.control_client import ControlClient, NonceStore
from tools.supervisor.supervisor import GroupOps, Supervisor, _AttachedHandle

MACHINE = "agent-e2e"
OPERATOR = "op@example.com"
INGEST = "e2e-ingest"
SUP_SECRET = "sup-secret"
RUNNER_SECRET = "runner-secret"
DEFAULT_PID = 4242
DEFAULT_STARTED_AT = "2026-07-30T09:15:00Z"
DEFAULT_EXE = "/usr/local/bin/codex"
DEFAULT_CMD = "codex session --tour"


def _keypair():
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        _ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())
    pub_bytes = priv.public_key().public_bytes(
        _ser.Encoding.Raw, _ser.PublicFormat.Raw)
    return priv_bytes, pub_bytes


def _encryption_key() -> bytes:
    return b"\xb1\xc2\xd3\xe4\xf5\x06\x17\x28\x39\x4a\x5b\x6c\x7d\x8e\x9f\xa0" \
           b"\x01\x12\x23\x34\x45\x56\x67\x78\x89\x9a\xab\xbc\xcd\xde\xef\x01"


def instance_row(**overrides):
    """A sanitized ``instances[]`` row (exactly INSTANCE_FIELDS keys)."""
    data = {
        "pid": DEFAULT_PID,
        "pgid": DEFAULT_PID,
        "exe_path": DEFAULT_EXE,
        "cmdline": DEFAULT_CMD,
        "agent_family": "codex",
        "native_file_path": None,
        "started_at": DEFAULT_STARTED_AT,
        "attachable": True,
    }
    data.update(overrides)
    return data


def snapshot(rows):
    return {"machine": MACHINE, "instances": rows}


def valid_candidate(**overrides):
    data = {"machine_id": MACHINE, "pid": DEFAULT_PID,
            "started_at": DEFAULT_STARTED_AT}
    data.update(overrides)
    return data


def operator_headers():
    return {"Cf-Access-Authenticated-User-Email": OPERATOR}


class FakeObservationRepo:
    def __init__(self, current):
        self._current = current

    def read_current(self, machine):
        return self._current


# --------------------------------------------------------------------------- #
# probe-side fakes — recording, zero real signals
# --------------------------------------------------------------------------- #


class _FakeProcess:
    """The mutable live process the identity reader mirrors.

    ``signals`` is the single array every recording surface shares, so one
    assertion covers every signal attempted on the seat.
    """

    def __init__(self, pid, started_at, exe_path):
        self.pid = pid
        self.started_at = started_at
        self.exe_path = exe_path
        self.signals = []
        self.group_signals = []

    def poll(self):
        return None  # the process is untouched — still alive


class _RecordingHandle(_AttachedHandle):
    """A REAL ``_AttachedHandle`` subclass recording any leaked signal."""

    def __init__(self, pid, started_at, exe_path, *, process=None, pgid=None):
        super().__init__(pid=int(pid), pgid=int(pgid if pgid is not None else pid),
                         started_at=started_at, exe_path=exe_path)
        self.proc = process
        self.signals = [] if process is None else process.signals
        self.group_signals = [] if process is None else process.group_signals


class _RecordingOps(GroupOps):
    """In-memory GroupOps: never touches the OS; records every signal."""

    def __init__(self, process):
        super().__init__(platform="test")
        self.process = process
        self.attach_calls = []
        self.closed = []

    def capability(self):
        return ("process_group_only", "1")

    def attach(self, pid, started_at, exe_path):
        handle = _RecordingHandle(pid, started_at, exe_path, process=self.process)
        self.attach_calls.append(handle)
        return handle

    def create(self, argv, cwd, env):
        raise RuntimeError("launched sessions are out of scope for the e2e")

    def proc_poll(self, handle):
        return None

    def proc_wait(self, handle, timeout=None):
        return None

    def group_alive(self, handle):
        return True

    def group_terminate(self, handle, sig):
        self.process.signals.append(sig)
        return True

    def group_kill(self, handle, sig):
        self.process.signals.append(sig)
        return True

    def group_reap(self, handle, timeout=None):
        return True

    def close(self, handle):
        self.closed.append(handle)


class _StubBridge:
    """A minimal SessionBridge-shaped stub (open/start/ingest/flush/close)."""

    def __init__(self, cfg):
        self.cfg = cfg

    def start(self):
        pass

    def ingest_native(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# app build (REAL hub via create_app)
# --------------------------------------------------------------------------- #


def _build_app(root: Path, *, adoption_enabled: bool):
    hub = HubStores(root / "state")
    signing_priv, signing_pub = _keypair()
    cfg = hub.config(
        root,
        ingest_token=INGEST,
        dev_operator=OPERATOR,
        runner_credentials={MACHINE: RUNNER_SECRET},
        project_whitelist={MACHINE: ["agent-fleet"]},
        session_repositories_enabled=True,
        session_encryption_raw=_encryption_key(),
        supervisor_enabled=True,
        supervisor_credentials={MACHINE: SUP_SECRET},
        supervisor_signing_raw=signing_priv,
        adoption_repositories_enabled=adoption_enabled,
    )
    observation = FakeObservationRepo(
        {**snapshot([instance_row()]), "machine": MACHINE})
    app = create_app(cfg, repositories={"observation": observation})
    # Flask 3.3's ``Flask.post``/``delete``/... are route-registration
    # decorators, not request helpers — calling them returns a wrapper
    # function.  Monkey-patch the instance so ``app.post`` (used by both the
    # golden ``app.post(...)`` assertions and the in-process transport adapter)
    # actually dispatches an HTTP request through the app's test client.
    _tester = app.test_client()
    for _method in ("get", "post", "delete", "put", "patch"):
        setattr(app, _method, getattr(_tester, _method))
    _drop_pages_catchall(app)
    return app


def _flask_transport(app):
    def transport(path: str, body, headers):
        resp = app.post(path, json=body, headers=headers)
        return (resp.status_code, resp.get_json())

    return transport


def _drop_pages_catchall(app):
    """Strip the SSR/asset ``/<path:filepath>`` catch-all from the harness app.

    The page route is GET/HEAD/OPTIONS only, so ANY absent non-GET path is
    reported by Werkzeug as 405 ``method_not_allowed`` instead of the honest
    404 ``not_found`` — exactly the ``test_http_contracts`` contract for the
    real app (POST to unknown ``/api/*`` 405s). The disabled golden asserts
    ``app.post("/api/adoptions", json={}).status_code == 404`` (an absent
    adoption route reads as 404, not 405), so the harness scoped to those
    golden assertions removes the catch-all rule before exercising the map.
    """
    from werkzeug.routing.map import StateMachineMatcher

    url_m = app.url_map
    url_m._rules_by_endpoint.pop("pages.frontend_asset", None)
    url_m._remap = True
    matcher = StateMachineMatcher(url_m._matcher.merge_slashes)
    for rule_group in url_m._rules_by_endpoint.values():
        for rule in rule_group:
            if not getattr(rule, "build_only", False):
                matcher.add(rule)
    url_m._matcher = matcher
    return app


# --------------------------------------------------------------------------- #
# e2e harness
# --------------------------------------------------------------------------- #


class _E2E:
    """The shared module driver coupling the REAL hub + REAL client + probe."""

    def __init__(self, app, tmp: Path):
        self.app = app
        self.tmp = tmp
        self.last_session_id = None
        services = app.extensions["fleet"]["services"]
        self.adoptions = services["adoptions"]
        self.sessions = services["sessions"]
        self.repo = self.adoptions.adoption_repo
        self.process = _FakeProcess(DEFAULT_PID, DEFAULT_STARTED_AT, DEFAULT_EXE)
        ops = _RecordingOps(self.process)
        self.sup = Supervisor(
            manifest_dir=str(tmp / "probe"), machine_id=MACHINE, ops=ops)
        self.sup._default_attach_probe = self._probe
        pub = services["supervisor"].public_key()
        self.client = ControlClient(
            hub_url="https://hub.invalid",
            machine_id=MACHINE,
            credential=SUP_SECRET,
            hub_public_key=pub,
            public_supervisor=self.sup,
            transport=_flask_transport(app),
            nonce_store=NonceStore(tmp / "used"),
            bridge_factory=lambda cfg: _StubBridge(cfg),
        )

    def _probe(self, pid_value):
        """The identity re-read: mirrors the mutable live process."""
        if int(pid_value) != int(self.process.pid):
            return None
        return (self.process.started_at, self.process.exe_path)

    # ---- golden helpers ---------------------------------------------------

    def operator_adopt(self):
        resp = self.app.post("/api/adoptions", json=valid_candidate(),
                             headers=operator_headers())
        assert resp.status_code == 200, resp.get_json()
        session_id = resp.get_json()["session_id"]
        self.last_session_id = session_id
        return self.repo.get(session_id)

    def adopted_session(self):
        row = self.operator_adopt()
        assert row.status == "pending"
        self.probe_poll_and_receipt("adopted")
        return row

    def probe_poll_and_receipt(self, _expected_reason=None):
        """One pull cycle; the client uploads every bounded receipt itself."""
        return self.client.poll_once()

    def operator_delete(self, session_id):
        resp = self.app.delete(f"/api/adoptions/{session_id}",
                               headers=operator_headers())
        assert resp.status_code == 202, resp.get_json()

    def operator_control(self, session_id, action):
        resp = self.app.post(
            f"/api/adoption/{session_id}/source/control",
            json={"action": action, "reason_code": "test"},
            headers=operator_headers())
        assert resp.status_code == 202, resp.get_json()

    def ingest_native_line(self, line, *, seq=1, session_id=None):
        # The golden bodies call ``ingest_native_line("hello")`` without a
        # session argument (verbatim from the brief); fall back to the harness's
        # current advertised seat set by the last operator adopting.
        sid = session_id if session_id is not None else self.last_session_id
        evt = {
            "schema_version": 1,
            "event_id": f"evt_e2e_{seq:04d}",
            "stream_id": "stream_e2e",
            "machine_id": MACHINE,
            "session_id": sid,
            "attempt_id": None,
            "process_group_id": "grp_e2e",
            "sequence": seq,
            "kind": "user_message",
            "capture_quality": "best_effort",
            "source": "bridge",
            "emitted_at": "2026-08-30T08:00:00Z",
            "payload": {"text": line, "is_complete": True},
        }
        result = self.sessions.ingest_events([evt])
        assert result["status"] != "rejected", result
        return result

    def read_redacted(self, session_id, limit=20):
        return self.sessions.transcript_repo.read_redacted(
            session_id, limit=limit)

    def audit(self):
        return self.adoptions.transcripts.read_audit()


def test_operator_terminate_receipt_closes_session_summary(e2e):
    row = e2e.adopted_session()
    e2e.ingest_native_line("running", session_id=row.session_id)
    repo = e2e.sessions.session_repo
    assert repo.get_session(row.session_id)["status"] == "running"
    response = e2e.app.post(
        f"/api/adoption/{row.session_id}/source/control",
        json={"action": "terminate_session", "reason_code": "test"},
        headers=operator_headers())
    assert response.status_code == 202
    cid = response.get_json()["command_id"]
    response = e2e.app.post('/api/supervisor/receipts', json={
        'command_id': cid, 'status': 'succeeded', 'reason': 'terminated_forced'},
        headers={'X-Supervisor-Credential': f'{MACHINE}:{SUP_SECRET}'})
    assert response.status_code == 200, response.get_json()
    closed = repo.get_session(row.session_id)
    assert closed['status'] == 'closed'
    assert closed['current_state'] == 'stopped'
    assert closed['finished_at']
    first_finished_at = closed['finished_at']
    repo.update_agent_state(row.session_id, 'running', 'native_transcript', 'medium', 999)
    assert repo.get_session(row.session_id)['current_state'] == 'stopped'
    replay = e2e.app.post('/api/supervisor/receipts', json={
        'command_id': cid, 'status': 'failed', 'reason': 'escape_unverified'},
        headers={'X-Supervisor-Credential': f'{MACHINE}:{SUP_SECRET}'})
    assert replay.status_code == 200
    assert replay.get_json()['receipt']['reason'] == 'terminated_forced'
    assert repo.get_session(row.session_id)['finished_at'] == first_finished_at


def test_failed_session_projection_recovers_on_poll_after_hub_restart(e2e, monkeypatch):
    import sqlite3
    from hub.application.control_router import SupervisorReceiptHook
    from hub.application.supervisor_service import SupervisorService
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository

    row = e2e.adopted_session()
    e2e.ingest_native_line("running", session_id=row.session_id)
    repo = e2e.sessions.session_repo
    response = e2e.app.post(
        f"/api/adoption/{row.session_id}/source/control",
        json={"action": "terminate_session", "reason_code": "test"},
        headers=operator_headers())
    cid = response.get_json()["command_id"]
    original = repo.finish_managed_session

    def fail(*args):
        raise sqlite3.OperationalError("projection store unavailable")

    monkeypatch.setattr(repo, "finish_managed_session", fail)
    response = e2e.app.post('/api/supervisor/receipts', json={
        'command_id': cid, 'status': 'succeeded', 'reason': 'terminated'},
        headers={'X-Supervisor-Credential': f'{MACHINE}:{SUP_SECRET}'})
    assert response.status_code == 503
    assert repo.get_session(row.session_id)['status'] == 'running'
    monkeypatch.setattr(repo, "finish_managed_session", original)
    current = e2e.app.extensions['fleet']['services']['supervisor']
    recovered = SupervisorReceiptHook(
        SupervisorService(signing_key=current._inner._signing_key,
                          command_repository=SupervisorCommandRepository(
                              current._inner._command_repository.db_path)),
        None, session_repo=repo)
    assert recovered.poll(MACHINE) == []
    closed = repo.get_session(row.session_id)
    assert closed['status'] == 'closed'
    assert closed['current_state'] == 'stopped'
    assert recovered._inner.pending_session_projections(MACHINE) == []


def test_termination_before_session_start_recovers_without_command_replay(e2e, monkeypatch):
    row = e2e.adopted_session()
    repo = e2e.sessions.session_repo
    assert repo.get_session(row.session_id) is None
    response = e2e.app.post(
        f"/api/adoption/{row.session_id}/source/control",
        json={"action": "terminate_session", "reason_code": "test"},
        headers=operator_headers())
    assert response.status_code == 202
    cid = response.get_json()["command_id"]
    supervisor = e2e.app.extensions['fleet']['services']['supervisor']
    now = [1000.0]
    monkeypatch.setattr(supervisor._inner, '_now_fn', lambda: now[0])
    response = e2e.app.post('/api/supervisor/receipts', json={
        'command_id': cid, 'status': 'succeeded', 'reason': 'terminated'},
        headers={'X-Supervisor-Credential': f'{MACHINE}:{SUP_SECRET}'})
    assert response.status_code == 200, response.get_json()
    assert supervisor.poll(MACHINE) == []
    assert supervisor._inner.pending_session_projections(MACHINE) == []
    now[0] += 30
    assert [command['command_id'] for command in
            supervisor._inner.pending_session_projections(MACHINE)] == [cid]
    e2e.ingest_native_line('late transcript', session_id=row.session_id)
    assert supervisor.poll(MACHINE) == []
    closed = repo.get_session(row.session_id)
    assert closed['status'] == 'closed'
    assert closed['current_state'] == 'stopped'
    assert supervisor._inner.pending_session_projections(MACHINE) == []


def test_detach_receipt_keeps_live_session_open(e2e):
    row = e2e.adopted_session()
    e2e.ingest_native_line("running", session_id=row.session_id)
    e2e.operator_delete(row.session_id)
    e2e.probe_poll_and_receipt()
    active = e2e.sessions.session_repo.get_session(row.session_id)
    assert active['status'] == 'running'
    assert active['finished_at'] is None
    assert e2e.process.signals == []


@pytest.mark.parametrize('action,status,reason', [
    ('pause_session', 'succeeded', 'paused'),
    ('resume_session', 'succeeded', 'running'),
    ('terminate_session', 'failed', 'escape_unverified'),
    ('terminate_session', 'succeeded', 'escape_unverified'),
    ('terminate_session', 'rejected', 'pid_reused'),
])
def test_non_termination_receipts_never_close_session(e2e, action, status, reason):
    row = e2e.adopted_session()
    e2e.ingest_native_line("running", session_id=row.session_id)
    response = e2e.app.post(
        f"/api/adoption/{row.session_id}/source/control",
        json={"action": action, "reason_code": "test"}, headers=operator_headers())
    cid = response.get_json()['command_id']
    for incoming_reason in (reason, 'terminated'):
        response = e2e.app.post('/api/supervisor/receipts', json={
            'command_id': cid, 'status': status, 'reason': incoming_reason},
            headers={'X-Supervisor-Credential': f'{MACHINE}:{SUP_SECRET}'})
        assert response.status_code == 200, response.get_json()
    active = e2e.sessions.session_repo.get_session(row.session_id)
    assert active['status'] == 'running'
    assert active['finished_at'] is None


@pytest.fixture
def e2e(tmp_path):
    _root = tmp_path
    app = _build_app(_root, adoption_enabled=True)
    yield _E2E(app, _root)


@pytest.fixture
def app(tmp_path):
    yield _build_app(tmp_path, adoption_enabled=False)


# --------------------------------------------------------------------------- #
# the four golden tests (verbatim from the brief)
# --------------------------------------------------------------------------- #


def test_adopted_receipt_promotes_and_observes(e2e):
    row = e2e.operator_adopt()
    assert e2e.repo.get(row.session_id).status == "pending"
    e2e.probe_poll_and_receipt("adopted")
    assert e2e.repo.get(row.session_id).status == "adopted"
    e2e.ingest_native_line("hello")
    assert e2e.read_redacted(row.session_id)


def test_revoke_detaches_without_signal(e2e):
    row = e2e.adopted_session()
    e2e.operator_delete(row.session_id)
    assert e2e.repo.get(row.session_id).status == "revoked"
    e2e.probe_poll_and_receipt("detached")
    assert e2e.process.poll() is None
    assert e2e.process.signals == []


def test_drift_revokes_and_never_terminates(e2e):
    row = e2e.adopted_session()
    e2e.process.started_at = "2026-09-01T00:00:00Z"
    e2e.operator_control(row.session_id, "terminate_session")
    e2e.probe_poll_and_receipt("pid_reused")
    assert e2e.repo.get(row.session_id).status == "revoked"
    assert e2e.process.signals == []
    assert any(a["action"] == "adoption_drift" for a in e2e.audit())


def test_disabled_flag_has_no_adoption_service(app):
    assert app.extensions["fleet"]["services"].get("adoptions") is None
    assert app.post("/api/adoptions", json={}).status_code == 404
