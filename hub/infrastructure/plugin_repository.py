"""Additive plugin settings, node offers and observations in platform.db."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import time

from fleet_core.plugins import PluginError
from hub.infrastructure.sqlite import connect


class PluginRepository:
    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path, self.clock = Path(db_path), clock

    def init(self):
        with closing(connect(self.db_path)) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS plugin_settings (
                    owner_id TEXT NOT NULL, plugin_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id,plugin_id));
                CREATE TABLE IF NOT EXISTS plugin_targets (
                    owner_id TEXT NOT NULL, plugin_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(owner_id,plugin_id,node_id));
                CREATE TABLE IF NOT EXISTS plugin_offers (
                    owner_id TEXT NOT NULL, node_id TEXT NOT NULL, plugin_id TEXT NOT NULL,
                    offer TEXT NOT NULL, reported_at REAL NOT NULL,
                    PRIMARY KEY(owner_id,node_id,plugin_id));
                CREATE TABLE IF NOT EXISTS plugin_observations (
                    owner_id TEXT NOT NULL, node_id TEXT NOT NULL, plugin_id TEXT NOT NULL,
                    context_id TEXT NOT NULL, hostname TEXT NOT NULL, value TEXT NOT NULL,
                    command_created_at REAL NOT NULL,
                    PRIMARY KEY(owner_id,node_id,plugin_id,context_id,hostname));
                CREATE INDEX IF NOT EXISTS idx_plugin_operations
                    ON platform_commands(owner_id,action,created_at DESC);
            ''')

    def setting(self, owner, plugin):
        with closing(connect(self.db_path)) as conn:
            row = conn.execute('SELECT enabled,revision FROM plugin_settings WHERE owner_id=? AND plugin_id=?',
                               (owner, plugin)).fetchone()
            return dict(enabled=bool(row['enabled']), revision=row['revision']) if row else dict(enabled=False, revision=0)

    def configure(self, owner, plugin, enabled, revision):
        if type(enabled) is not bool or type(revision) is not int or revision < 0:
            raise PluginError('invalid_plugin_settings')
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT revision FROM plugin_settings WHERE owner_id=? AND plugin_id=?',
                               (owner, plugin)).fetchone()
            if (row['revision'] if row else 0) != revision:
                raise PluginError('revision_conflict')
            conn.execute('INSERT INTO plugin_settings VALUES(?,?,?,?) ON CONFLICT(owner_id,plugin_id) '
                         'DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision',
                         (owner, plugin, int(enabled), revision + 1))
        return dict(enabled=enabled, revision=revision + 1)

    def set_target(self, owner, plugin, node, enabled):
        if type(enabled) is not bool:
            raise PluginError('invalid_plugin_settings')
        with closing(connect(self.db_path)) as conn:
            conn.execute('INSERT INTO plugin_targets VALUES(?,?,?,?) ON CONFLICT(owner_id,plugin_id,node_id) '
                         'DO UPDATE SET enabled=excluded.enabled', (owner, plugin, node, int(enabled)))

    def report(self, owner, node, offers):
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            # One node reports its full current inventory, including removals.
            conn.execute('DELETE FROM plugin_offers WHERE owner_id=? AND node_id=?', (owner, node))
            for offer in offers:
                conn.execute('INSERT INTO plugin_offers VALUES(?,?,?,?,?)',
                             (owner, node, offer['plugin_id'], json.dumps(offer), self.clock()))

    def node_data(self, owner, plugin):
        with closing(connect(self.db_path)) as conn:
            targets = {r['node_id']: bool(r['enabled']) for r in conn.execute(
                'SELECT * FROM plugin_targets WHERE owner_id=? AND plugin_id=?', (owner, plugin))}
            offers = {r['node_id']: {**json.loads(r['offer']), 'reported_at': r['reported_at']}
                      for r in conn.execute('SELECT * FROM plugin_offers WHERE owner_id=? AND plugin_id=?', (owner, plugin))}
            observations = {}
            for row in conn.execute('SELECT node_id,value FROM plugin_observations WHERE owner_id=? AND plugin_id=?',
                                    (owner, plugin)):
                observations.setdefault(row['node_id'], []).append(json.loads(row['value']))
            return targets, offers, observations

    def operations(self, owner, plugin, *, node_id=None, context_id=None, hostname=None, limit=50):
        from hub.infrastructure.command_repository import CommandRepository
        query = 'SELECT * FROM platform_commands WHERE owner_id=? AND action GLOB ?'
        params = [owner, 'plugin.' + plugin + '.*']
        if node_id:
            query += ' AND target_node=?'
            params.append(node_id)
        if context_id is not None:
            query += " AND resource_id=? AND json_extract(arguments,'$.hostname')=?"
            params.extend((context_id, hostname))
        query += ' ORDER BY created_at DESC,command_id DESC LIMIT ?'
        params.append(limit)
        with closing(connect(self.db_path)) as conn:
            return [CommandRepository._decode(row) for row in conn.execute(query, params)]

    def current_login(self, owner, plugin, node, context, hostname):
        """Recover a login independently of the bounded recent-history page."""
        from hub.infrastructure.command_repository import CommandRepository
        with closing(connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM platform_commands WHERE owner_id=? AND target_node=? AND action=? "
                "AND resource_id=? AND json_extract(arguments,'$.hostname')=? "
                "AND status NOT IN ('failed','expired') ORDER BY created_at DESC,command_id DESC LIMIT 1",
                (owner, node, 'plugin.' + plugin + '.login_start', context, hostname)).fetchone()
            if not row:
                return None
            start = CommandRepository._decode(row)
            job = (start.get('result') or {}).get('result') or {
                'login_id': start['command_id'], 'context_id': context,
                'hostname': hostname, 'state': 'recovering'}
            progress = conn.execute(
                "SELECT * FROM platform_commands WHERE owner_id=? AND target_node=? "
                "AND action IN (?,?) AND json_extract(arguments,'$.login_id')=? "
                "AND (status='succeeded' OR (status='failed' AND json_extract(result,'$.error_code')='login_not_found')) "
                "ORDER BY created_at DESC,command_id DESC LIMIT 1",
                (owner, node, 'plugin.' + plugin + '.login_status', 'plugin.' + plugin + '.login_cancel',
                 start['command_id'])).fetchone()
            if progress:
                receipt = CommandRepository._decode(progress).get('result') or {}
                if receipt.get('error_code') == 'login_not_found':
                    job = {**job, 'state': 'unknown', 'error_code': 'login_not_found'}
                elif receipt.get('result', {}).get('login_id') == start['command_id']:
                    job = receipt['result']
            return {**job, 'node_id': node}

    def observe(self, owner, node, plugin, context, hostname, value, created):
        with closing(connect(self.db_path)) as conn:
            conn.execute('INSERT INTO plugin_observations VALUES(?,?,?,?,?,?,?) '
                         'ON CONFLICT(owner_id,node_id,plugin_id,context_id,hostname) DO UPDATE '
                         'SET value=excluded.value,command_created_at=excluded.command_created_at '
                         'WHERE excluded.command_created_at>=plugin_observations.command_created_at',
                         (owner, node, plugin, context, hostname, json.dumps(value), created))
