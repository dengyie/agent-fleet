"""Require actual passing results for every declared CI journey selector."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest


def pytest_addoption(parser):
    group = parser.getgroup('journeys')
    group.addoption('--require-journeys', action='store_true', help='Fail if any required journey is missing, skipped, xfailed or failed')
    group.addoption('--journey-matrix', default='docs/testing/journeys.json')
    group.addoption('--journey-report', help='Write secret-free journey evidence JSON')


def pytest_configure(config):
    if not (config.getoption('--require-journeys') or config.getoption('--journey-report')):
        return
    matrix_path = Path(config.getoption('--journey-matrix'))
    try:
        raw = matrix_path.read_bytes()
        matrix = json.loads(raw)
        journeys = matrix['journeys']
        if matrix['schema_version'] != 1 or not journeys:
            raise ValueError()
        seen = set()
        for row in journeys:
            if row['id'] in seen or not row['acceptance'] or not row['tests'] or row['layer'] not in ('browser', 'integration', 'contract'):
                raise ValueError()
            seen.add(row['id'])
            for target in row['tests']:
                if not isinstance(target, str) or '::test_' not in target or not target.startswith('tests/'):
                    raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise pytest.UsageError('Invalid or missing journey matrix: ' + str(matrix_path)) from None
    plugin = JourneyEvidence(config, matrix, hashlib.sha256(raw).hexdigest())
    config.pluginmanager.register(plugin, 'journey-evidence')


class JourneyEvidence:
    def __init__(self, config, matrix, digest):
        self.config, self.matrix, self.digest = config, matrix, digest
        self.selected = set()
        self.deselected = set()
        self.phases = {}
        self.nonpassing = set()

    def pytest_deselected(self, items):
        self.deselected.update(item.nodeid for item in items)

    def pytest_collection_finish(self, session):
        self.selected = {item.nodeid for item in session.items} | self.deselected

    def pytest_runtest_logreport(self, report):
        self.phases.setdefault(report.nodeid, {})[report.when] = report.outcome
        if report.outcome != 'passed' or hasattr(report, 'wasxfail'):
            self.nonpassing.add(report.nodeid)

    def rows(self):
        result = []
        for journey in self.matrix['journeys']:
            selectors = []
            for selector in journey['tests']:
                cases = sorted(n for n in self.selected if n == selector or n.startswith(selector + '['))
                passed = [n for n in cases if n not in self.nonpassing and
                          all(self.phases.get(n, {}).get(phase) == 'passed' for phase in ('setup', 'call', 'teardown'))]
                selectors.append({'selector': selector, 'collected': len(cases), 'passed': len(passed),
                                  'status': 'passed' if cases and len(passed) == len(cases) else 'incomplete',
                                  'nonpassing_cases': [n for n in cases if n not in passed]})
            result.append({'id': journey['id'], 'layer': journey['layer'],
                           'status': 'passed' if all(s['status'] == 'passed' for s in selectors) else 'incomplete',
                           'selectors': selectors})
        return result

    def pytest_sessionfinish(self, session, exitstatus):
        rows = self.rows()
        incomplete = [row['id'] for row in rows if row['status'] != 'passed']
        if incomplete and self.config.getoption('--require-journeys'):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        path = self.config.getoption('--journey-report')
        if path:
            revision = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
            dirty = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
            report = {'schema_version': 1, 'revision': revision.stdout.strip() if revision.returncode == 0 else None,
                      'working_tree_dirty': bool(dirty.stdout.strip()), 'matrix_sha256': self.digest,
                      'ci_status': 'passed' if not incomplete and int(exitstatus) == 0 else 'failed',
                      'journeys': rows,
                      'external_checks': [{'id': item['id'], 'status': 'not_run'} for item in self.matrix.get('external_checks', [])]}
            target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def pytest_terminal_summary(self, terminalreporter):
        rows = self.rows()
        failed = [row['id'] for row in rows if row['status'] != 'passed']
        terminalreporter.write_sep('=', f"Journey coverage: {len(rows) - len(failed)}/{len(rows)} passing; external checks not run")
        if failed:
            terminalreporter.write_line('Incomplete journeys: ' + ', '.join(failed), red=True)
