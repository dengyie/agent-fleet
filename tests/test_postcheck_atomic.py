"""Postcheck evidence and its audit must survive storage faults together."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from flask import Flask
import pytest

from hub.application.command_postcheck_service import CommandPostcheckService
from hub.application.task_service import ApplicationError
from hub.infrastructure.command_repository import CommandRepository, CommandRepositoryError
from test_platform_postcheck import OWNER, _app as workspace_app, _unknown_write
from test_platform_service_postcheck import _app as service_app, _unknown_inspect
from test_platform_service_logs_postcheck import _unknown_logs

KINDS = ('workspace_digest', 'service_inspect', 'service_logs')
STATES = ('matched', 'mismatch', 'remains_unknown')
SOURCES = {'workspace_digest': 'workspace_check', 'service_inspect': 'service_check',
    'service_logs': 'service_logs_check'}


@dataclass(frozen=True, slots=True)
class Case:
    app: Flask
    repository: CommandRepository
    service: CommandPostcheckService
    kind: str
    table: str
    check_id: str
    state: str
    result: dict[str, Any]

    def descriptor(self) -> dict[str, Any]:
        with closing(self.repository._connect()) as connection:
            return dict(connection.execute('SELECT * FROM ' + self.table + ' WHERE command_id=?', ('original',)).fetchone())

    def audits(self) -> list[dict[str, Any]]:
        with closing(self.repository._connect()) as connection:
            return [dict(row) for row in connection.execute(
                'SELECT * FROM platform_command_reconciliations WHERE command_id=?', ('original',))]

    def restart(self) -> CommandPostcheckService:
        original = self.service
        return CommandPostcheckService(CommandRepository(self.repository.db_path), original.delivery,
            service_repository=original.service_repository, service_health=original.service_health,
            clock=original.clock)

    def finish(self, *, state: str | None = None, result: dict[str, Any] | None = None) -> dict[str, Any]:
        method = self.repository.finish_postcheck if self.kind == 'workspace_digest' else self.repository.finish_service_postcheck
        return method(OWNER, 'original', kind=self.kind, state=state or self.state,
            result=self.result if result is None else result, evidence_id='postcheck-' + self.check_id, now=200)


def _prepare(root: Path, kind: str, outcome: str = 'matched') -> Case:
    if kind == 'workspace_digest':
        app = workspace_app(root)
        _, repository = _unknown_write(app, command_id='original')
        result = {'path': 'report.md', 'sha256': hashlib.sha256(b'hello').hexdigest(), 'size': 5}
        if outcome == 'mismatch':
            result['sha256'] = hashlib.sha256(b'different').hexdigest()
        table = 'platform_command_postchecks'
    elif kind == 'service_inspect':
        app = service_app(root)
        _, repository, _ = _unknown_inspect(app, command_id='original')
        result = {'state': 'healthy', 'returncode': 0, 'duration_s': 0}
        table = 'platform_service_postchecks'
    else:
        app = service_app(root)
        repository, _ = _unknown_logs(app, command_id='original')
        result = {'text': 'fixed safe log', 'truncated': False, 'observed_at': 100}
        table = 'platform_service_postchecks'
    response = app.test_client().post('/api/platform/v1/commands/original/postcheck')
    assert response.status_code == 202
    check_id = response.json['postcheck']['check_command_id']
    claims = repository.claim_for_node('node-a', 'fault-worker', owner_id=OWNER)
    assert len(claims) == 1 and claims[0]['command_id'] == check_id
    receipt = {'result': result} if outcome != 'remains_unknown' else {'error_code': 'not_found'}
    repository.record_receipt(check_id, 'succeeded' if outcome != 'remains_unknown' else 'failed',
        result=receipt, worker_id='fault-worker', owner_id=OWNER, node_id='node-a')
    saved_result = result if outcome != 'remains_unknown' else {'error_code': 'not_found'}
    return Case(app, repository, app.extensions['fleet']['services']['command_postcheck'],
        kind, table, check_id, outcome, saved_result)


def _fault(case: Case, operation: str) -> None:
    table = 'platform_command_reconciliations' if operation == 'INSERT' else case.table
    with closing(case.repository._connect()) as connection:
        connection.executescript(
            'CREATE TABLE postcheck_fault (enabled INTEGER NOT NULL); '
            'INSERT INTO postcheck_fault VALUES(1); '
            'CREATE TRIGGER block_postcheck_completion BEFORE ' + operation + ' ON ' + table +
            " WHEN (SELECT enabled FROM postcheck_fault)=1 BEGIN SELECT RAISE(ABORT, 'postcheck_storage_fault'); END;")


@pytest.mark.parametrize('kind,outcome', [(kind, outcome) for kind in KINDS
    for outcome in ('matched', 'remains_unknown')] + [('workspace_digest', 'mismatch')])
@pytest.mark.parametrize('operation', ['INSERT', 'UPDATE'])
def test_postcheck_storage_failure_rolls_back_and_restart_finishes_once(
        tmp_path: Path, kind: str, outcome: str, operation: str) -> None:
    case = _prepare(tmp_path, kind, outcome)
    original, check, descriptor = case.repository.get('original'), case.repository.get(case.check_id), case.descriptor()
    _fault(case, operation)
    with pytest.raises(ApplicationError) as raised:
        case.service.get_any(OWNER, 'original')
    assert raised.value.code in {'command_store', 'reconciliation_conflict'}
    assert case.descriptor() == descriptor
    assert case.audits() == []
    assert isinstance(raised.value.__cause__, CommandRepositoryError)
    assert isinstance(raised.value.__cause__.__cause__, sqlite3.Error)
    assert case.repository.get('original') == original and case.repository.get(case.check_id) == check
    with closing(case.repository._connect()) as connection:
        connection.execute('UPDATE postcheck_fault SET enabled=0')
    restarted = case.restart()
    assert restarted.get_any(OWNER, 'original')['postcheck']['state'] == outcome
    assert restarted.get_any(OWNER, 'original')['postcheck']['state'] == outcome
    audits = case.audits()
    assert len(audits) == 1
    assert audits[0]['outcome'] == ('confirmed_succeeded' if outcome == 'matched' else 'remains_unknown')
    assert audits[0]['evidence_source'] == SOURCES[kind]
    assert case.repository.get('original') == original and case.repository.get(case.check_id) == check


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('state', STATES)
def test_terminal_postcheck_repairs_legacy_audit_from_saved_evidence(
        tmp_path: Path, kind: str, state: str) -> None:
    case = _prepare(tmp_path, kind)
    original, check = case.repository.get('original'), case.repository.get(case.check_id)
    with closing(case.repository._connect()) as connection:
        connection.execute('UPDATE ' + case.table + ' SET state=?,result=?,evidence_id=?,updated_at=? WHERE command_id=?',
            (state, json.dumps(case.result), 'postcheck-' + case.check_id, 150, 'original'))
    saved = case.descriptor()
    assert case.audits() == []
    restarted = case.restart()
    if kind != 'workspace_digest':
        case.service.service_health.register(OWNER, {'service_id': 'api', 'node_id': 'node-a',
            'adapter': 'systemd', 'target_alias': 'changed.service', 'allowed_actions': ['inspect']})
    response = restarted.get_any(OWNER, 'original')
    assert response['postcheck']['state'] == state
    assert case.descriptor() == saved
    audit = case.audits()
    assert len(audit) == 1 and audit[0]['evidence_reference'] == saved['evidence_id']
    assert audit[0]['outcome'] == ('confirmed_succeeded' if state == 'matched' else 'remains_unknown')
    assert audit[0]['evidence_source'] == SOURCES[kind]
    assert case.repository.get('original') == original and case.repository.get(case.check_id) == check


@pytest.mark.parametrize('kind', KINDS)
def test_conflicting_postcheck_audit_rolls_back_completion(tmp_path: Path, kind: str) -> None:
    case = _prepare(tmp_path, kind)
    before = case.descriptor()
    case.repository.record_reconciliation(OWNER, 'original', actor='operator', outcome='remains_unknown',
        evidence_source='operator_note', evidence_reference='other-evidence',
        reconciliation_id='postcheck-' + case.check_id, now=150)
    with pytest.raises(ApplicationError) as raised:
        case.service.get_any(OWNER, 'original')
    assert raised.value.code == 'reconciliation_conflict'
    assert case.descriptor() == before and len(case.audits()) == 1
    assert case.audits()[0]['actor'] == 'operator'


@pytest.mark.parametrize('kind', KINDS)
def test_stale_postcheck_completion_returns_saved_terminal_and_audit(tmp_path: Path, kind: str) -> None:
    case = _prepare(tmp_path, kind)
    case.service.get_any(OWNER, 'original')
    saved, audit = case.descriptor(), case.audits()
    response = case.finish(state='mismatch', result={'error_code': 'stale-proposal'})
    assert response['state'] == 'matched'
    assert case.descriptor() == saved and case.audits() == audit


@pytest.mark.parametrize('kind', KINDS)
def test_concurrent_postcheck_refreshes_commit_one_terminal_audit(tmp_path: Path, kind: str) -> None:
    case = _prepare(tmp_path, kind)
    original, check = case.repository.get('original'), case.repository.get(case.check_id)
    services = [case.restart() for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(service.get_any, OWNER, 'original') for service in services]
        responses = [future.result(timeout=10) for future in futures]
    assert all(response['postcheck']['state'] == 'matched' for response in responses)
    assert len(case.audits()) == 1
    assert case.repository.get('original') == original and case.repository.get(case.check_id) == check


@pytest.mark.parametrize('kind', KINDS)
def test_completed_postcheck_reads_have_no_write_transaction(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _prepare(tmp_path, kind)
    case.service.get_any(OWNER, 'original')
    repository = CommandRepository(case.repository.db_path)
    service = CommandPostcheckService(repository, case.service.delivery,
        service_repository=case.service.service_repository, service_health=case.service.service_health)
    original_connect = repository._connect
    statements: list[str] = []

    def traced_connection() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(repository, '_connect', traced_connection)
    assert service.get_any(OWNER, 'original')['postcheck']['state'] == 'matched'
    assert not any(statement.lstrip().upper().startswith(('BEGIN', 'INSERT', 'UPDATE', 'REPLACE')) for statement in statements)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('invalid', ['wrong-evidence', 'nonfinite-result', 'oversized-result', 'invalid-time'])
def test_invalid_postcheck_completion_never_writes_descriptor_or_audit(tmp_path: Path, kind: str, invalid: str) -> None:
    case = _prepare(tmp_path, kind)
    before = case.descriptor()
    options: dict[str, Any] = {'kind': kind, 'state': 'matched', 'result': case.result,
        'evidence_id': 'postcheck-' + case.check_id, 'now': 200}
    if invalid == 'wrong-evidence':
        options['evidence_id'] = 'other-evidence'
    elif invalid == 'nonfinite-result':
        options['result'] = {'size': float('nan')}
    elif invalid == 'oversized-result':
        options['result'] = {'text': 'x' * 16384}
    else:
        options['now'] = float('inf')
    method = case.repository.finish_postcheck if kind == 'workspace_digest' else case.repository.finish_service_postcheck
    with pytest.raises(CommandRepositoryError):
        method(OWNER, 'original', **options)
    assert case.descriptor() == before and case.audits() == []


def test_reconciliation_storage_failure_preserves_sqlite_cause(tmp_path: Path) -> None:
    case = _prepare(tmp_path, 'workspace_digest')
    _fault(case, 'INSERT')
    with pytest.raises(CommandRepositoryError) as raised:
        case.repository.record_reconciliation(OWNER, 'original', actor='operator', outcome='remains_unknown',
            evidence_source='operator_note', evidence_reference='fixed', reconciliation_id='operator-evidence', now=150)
    assert isinstance(raised.value.__cause__, sqlite3.IntegrityError)
    assert case.audits() == []
