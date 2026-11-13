"""Review regression: rollback after a schema-bumping release restores databases."""
import json
import sqlite3

import pytest

from deploy import auto_release as release
from tests.test_auto_deploy import NEW, OLD, Hooks, assert_runtime_preserved, deployment  # noqa: F401


def _set_schema(db_path, version):
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(version),))


class MigratingHooks(Hooks):
    """New code migrates schema 3 -> 4 on start, then the public check fails."""

    def start(self):
        super().start()
        db_path = self.live / 'var/data.db'
        if 'commit: ' + NEW in (self.live / 'RELEASE_ORIGIN').read_text():
            _set_schema(db_path, 4)
            with sqlite3.connect(db_path) as db:
                db.execute("INSERT INTO records VALUES ('written-by-new')")
                db.execute('PRAGMA journal_mode=WAL')
        else:
            # Old code refuses a newer schema (platform_db unsupported_schema_version).
            with sqlite3.connect(db_path) as db:
                version = int(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
            if version > 3:
                raise RuntimeError('unsupported_schema_version')

    def public_verify(self, sha):
        super().public_verify(sha)
        if sha == NEW:
            raise RuntimeError('public_verify_failed')


@pytest.fixture
def migrating(deployment):  # noqa: F811
    manager, stage, _hooks = deployment
    _set_schema(manager.live / 'var/data.db', 3)
    hooks = MigratingHooks(manager.live)
    manager.hooks = hooks
    return manager, stage, hooks


def test_schema_bump_then_late_failure_restores_database_and_old_code_starts(migrating):
    manager, stage, hooks = migrating
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    assert OLD in (manager.live / 'RELEASE_ORIGIN').read_text()
    assert_runtime_preserved(manager.live)
    with sqlite3.connect(manager.live / 'var/data.db') as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ('3',)
        assert db.execute('SELECT value FROM records').fetchall() == [('before',)]
    assert hooks.calls[-1] == 'publish:old'
    backups = [p for p in manager.state.iterdir() if p.is_dir()]
    result = json.loads((backups[0] / 'result.json').read_text())
    assert result['phase'] == 'rolled_back'
    assert result['restored_databases'] == ['var/data.db']
    # The failed release's writes are kept aside for manual recovery.
    with sqlite3.connect(backups[0] / 'failed-databases/var/data.db') as db:
        assert ('written-by-new',) in db.execute('SELECT value FROM records').fetchall()


def test_unchanged_schema_keeps_new_writes(deployment):  # noqa: F811
    manager, stage, hooks = deployment
    hooks.fail_new = True
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    with sqlite3.connect(manager.live / 'var/data.db') as db:
        assert db.execute('SELECT value FROM records').fetchall() == [('before',), ('after-start',)]
    backup = next(p for p in manager.state.iterdir() if p.is_dir())
    assert 'restored_databases' not in json.loads((backup / 'result.json').read_text())
    assert not (backup / 'failed-databases').exists()


class DomainMigratingHooks(Hooks):
    def start(self):
        super().start()
        with sqlite3.connect(self.live / 'var/data.db') as db:
            if 'commit: ' + NEW in (self.live / 'RELEASE_ORIGIN').read_text():
                db.execute('UPDATE workflow_schema_meta SET minimum_reader_version=16')
                db.execute("INSERT INTO records VALUES ('written-by-domain-upgrade')")
            elif db.execute('SELECT minimum_reader_version FROM workflow_schema_meta').fetchone()[0] > 15:
                raise RuntimeError('unsupported_workflow_reader')

    def public_verify(self, sha):
        super().public_verify(sha)
        if sha == NEW: raise RuntimeError('public_verify_failed')


def test_domain_reader_upgrade_restores_compatible_database_and_preserves_failed_writes(deployment):  # noqa: F811
    manager, stage, _ = deployment
    _set_schema(manager.live / 'var/data.db', 3)
    with sqlite3.connect(manager.live / 'var/data.db') as db:
        db.execute('CREATE TABLE workflow_schema_meta(singleton INTEGER PRIMARY KEY,schema_version INTEGER,minimum_reader_version INTEGER,admission_mode TEXT)')
        db.execute("INSERT INTO workflow_schema_meta VALUES(1,3,15,'open')")
    manager.hooks = DomainMigratingHooks(manager.live)
    with pytest.raises(release.DeployError, match='rolled_back'):
        manager.apply(stage, NEW, '100')
    backup = next(p for p in manager.state.iterdir() if p.is_dir())
    with sqlite3.connect(manager.live / 'var/data.db') as db:
        assert db.execute('SELECT minimum_reader_version FROM workflow_schema_meta').fetchone()[0] == 15
    with sqlite3.connect(backup / 'failed-databases/var/data.db') as db:
        assert db.execute('SELECT minimum_reader_version FROM workflow_schema_meta').fetchone()[0] == 16
        assert ('written-by-domain-upgrade',) in db.execute('SELECT value FROM records').fetchall()


@pytest.mark.parametrize('table', ['workflow_summary_schema_meta', 'mcp_catalog_meta'])
def test_first_optional_domain_schema_is_detected_without_base_schema_change(tmp_path, table):
    live, saved = tmp_path / 'live', tmp_path / 'saved'
    live.mkdir(); saved.mkdir()
    for root in (live, saved): _set_schema(root / 'data.db', 3)
    with sqlite3.connect(live / 'data.db') as db:
        db.execute(f'CREATE TABLE {table}(singleton INTEGER PRIMARY KEY,schema_version INTEGER,minimum_reader_version INTEGER)')
        db.execute(f'INSERT INTO {table} VALUES(1,2,2)')
    assert release.changed_schema_databases(live, saved, ['data.db']) == ['data.db']


def test_operational_metadata_changes_do_not_restore_and_erase_new_business_writes(tmp_path):
    live, saved = tmp_path / 'live', tmp_path / 'saved'
    live.mkdir(); saved.mkdir()
    for root in (live, saved):
        _set_schema(root / 'data.db', 3)
        with sqlite3.connect(root / 'data.db') as db:
            db.execute('CREATE TABLE workflow_schema_meta(singleton INTEGER PRIMARY KEY,schema_version INTEGER,minimum_reader_version INTEGER,admission_mode TEXT)')
            db.execute("INSERT INTO workflow_schema_meta VALUES(1,3,16,'open')")
            db.execute('CREATE TABLE capability_schema_meta(singleton INTEGER PRIMARY KEY,schema_version INTEGER,minimum_reader_version INTEGER,report_sequence INTEGER)')
            db.execute('INSERT INTO capability_schema_meta VALUES(1,1,1,0)')
    with sqlite3.connect(live / 'data.db') as db:
        db.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
        db.execute('UPDATE capability_schema_meta SET report_sequence=99')
    assert release.changed_schema_databases(live, saved, ['data.db']) == []
