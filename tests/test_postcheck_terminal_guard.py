"""Terminal postcheck reads keep the original unknown and owner boundary."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from hub.application.task_service import ApplicationError
from hub.infrastructure.command_repository import CommandRepositoryError
from test_platform_postcheck import OWNER
from test_postcheck_atomic import KINDS, _prepare


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('change', ['owner_changed', 'status_changed'])
@pytest.mark.parametrize('has_audit', [True, False])
def test_terminal_postcheck_requires_original_unknown_and_owner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, change: str, has_audit: bool) -> None:
    case = _prepare(tmp_path, kind)
    if has_audit:
        assert case.service.get_any(OWNER, 'original')['postcheck']['state'] == 'matched'
    else:
        encoded = json.dumps(case.result, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with closing(case.repository._connect()) as connection:
            connection.execute('UPDATE ' + case.table + ' SET state=?,result=?,evidence_id=?,updated_at=? '
                'WHERE owner_id=? AND command_id=? AND kind=?',
                ('matched', encoded, 'postcheck-' + case.check_id, 200, OWNER, 'original', kind))
    before, audits = case.descriptor(), case.audits()
    assert len(audits) == int(has_audit)
    with closing(case.repository._connect()) as connection:
        if change == 'owner_changed':
            connection.execute('UPDATE platform_commands SET owner_id=? WHERE command_id=?',
                ('other@example.test', 'original'))
        else:
            connection.execute('UPDATE platform_commands SET status=? WHERE command_id=?', ('succeeded', 'original'))
    original = case.repository.get('original')
    check = case.repository.get(case.check_id)
    code = 'command_not_found' if change == 'owner_changed' else 'reconcile_requires_unknown'
    statements: list[str] = []
    connect = case.repository._connect

    def traced_connection() -> sqlite3.Connection:
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(case.repository, '_connect', traced_connection)
    with pytest.raises(CommandRepositoryError) as raised:
        case.finish()
    assert raised.value.code == code
    with pytest.raises(ApplicationError) as application:
        case.restart().get_any(OWNER, 'original')
    assert application.value.code == code
    assert isinstance(application.value.__cause__, CommandRepositoryError)
    assert application.value.__cause__.code == code
    response = case.app.test_client().get('/api/platform/v1/commands/original/postcheck')
    assert response.status_code == (404 if change == 'owner_changed' else 409)
    assert 'postcheck' not in response.json
    assert case.descriptor() == before and case.audits() == audits
    assert case.repository.get('original') == original and case.repository.get(case.check_id) == check
    forbidden = ('BEGIN', 'INSERT', 'UPDATE', 'REPLACE') if has_audit else ('INSERT', 'UPDATE', 'REPLACE')
    assert not any(statement.lstrip().upper().startswith(forbidden) for statement in statements)
