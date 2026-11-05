"""Unit tests for hub/notifier.py alert evaluation and stdout notifier."""
from pathlib import Path

import pytest

from hub import notifier

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def thresholds(monkeypatch):
    values = {'high_load': 4.0, 'disk_high_pct': 90}
    monkeypatch.setattr(notifier, '_thresholds', lambda: values)
    return values


def test_thresholds_read_from_hosts_yaml(tmp_path, monkeypatch):
    (tmp_path / 'hosts.yaml').write_text('thresholds:\n  high_load: 2.5\n  disk_high_pct: 70\n')
    monkeypatch.setattr(notifier, 'FLEET_HOME', tmp_path)
    assert notifier._thresholds() == {'high_load': 2.5, 'disk_high_pct': 70}


def test_thresholds_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, 'FLEET_HOME', tmp_path)
    assert notifier._thresholds() == notifier.DEFAULTS  # missing file
    (tmp_path / 'hosts.yaml').write_text('machines: []\n')
    assert notifier._thresholds() == notifier.DEFAULTS  # no thresholds key
    (tmp_path / 'hosts.yaml').write_text('thresholds: [unclosed\n')
    assert notifier._thresholds() == notifier.DEFAULTS  # invalid YAML


def test_repository_hosts_yaml_thresholds_cover_every_reader_key():
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / 'hosts.yaml').read_text())
    assert set(cfg['thresholds']) == set(notifier.DEFAULTS)


def test_quiet_snapshot_has_no_alerts(thresholds):
    snapshot = {'system': {'load': '1.5', 'disk_used_pct': '40%'},
                'agents': {'hermes': {'installed': True, 'gateway_state': 'running'},
                           'codex': {'error': 'empty log'}}}
    assert notifier.evaluate_snapshot('mac', snapshot) == []
    assert notifier.evaluate_snapshot('mac', {}) == []


def test_load_and_disk_alerts_use_thresholds(thresholds):
    alerts = notifier.evaluate_snapshot('mac', {'system': {'load': '4.5', 'disk_used_pct': '95%'}})
    assert alerts == ['mac 负载偏高: 4.5 (阈值 4.0)', 'mac 磁盘使用率 95.0% > 90%']
    thresholds['high_load'] = 5.0
    assert notifier.evaluate_snapshot('mac', {'system': {'load': 4.5}}) == []


def test_unparseable_system_values_are_ignored(thresholds):
    snapshot = {'system': {'load': 'n/a', 'disk_used_pct': 'unknown'}}
    assert notifier.evaluate_snapshot('mac', snapshot) == []


def test_hermes_gateway_and_collector_errors(thresholds):
    snapshot = {'agents': {
        'hermes': {'installed': True, 'gateway_state': 'stopped'},
        'claude': {'error': 'permission denied'},
        'ignored': 'not-a-dict',
    }}
    assert notifier.evaluate_snapshot('hk', snapshot) == [
        'hk Hermes gateway 状态异常: stopped',
        'hk claude 采集错误: permission denied',
    ]
    # Hermes that is not installed is not monitored.
    assert notifier.evaluate_snapshot('hk', {'agents': {'hermes': {'installed': False}}}) == []
    # Missing gateway state on an installed Hermes is reported as unknown.
    assert notifier.evaluate_snapshot('hk', {'agents': {'hermes': {'installed': True}}}) == [
        'hk Hermes gateway 状态异常: unknown']


def test_notify_callback_prints_only_alerting_state_changes(thresholds, capsys):
    callback = notifier.make_notify_cb()
    busy = {'system': {'load': 9}}
    callback({'event': 'heartbeat', 'machine': 'mac', 'extra': {'snapshot': busy}})
    callback({'event': 'state_changed', 'machine': 'mac', 'extra': {'snapshot': {}}})
    callback({'event': 'state_changed', 'machine': 'mac'})
    assert capsys.readouterr().out == ''
    callback({'event': 'state_changed', 'machine': 'mac', 'extra': {'snapshot': busy}})
    assert capsys.readouterr().out == '[AGENT-FLEET][ALERT] mac 负载偏高: 9.0 (阈值 4.0)\n'


def test_setup_alert_notify_subscribes_to_given_publisher(thresholds, capsys):
    class Publisher:
        def __init__(self):
            self.subscribers = []

        def subscribe(self, callback):
            self.subscribers.append(callback)

    publisher = Publisher()
    notifier.setup_alert_notify(publisher)
    assert len(publisher.subscribers) == 1
    publisher.subscribers[0]({'event': 'state_changed', 'machine': 'm',
                              'extra': {'snapshot': {'system': {'disk_used_pct': 99}}}})
    assert 'm 磁盘使用率 99.0% > 90%' in capsys.readouterr().out


def test_setup_alert_notify_defaults_to_global_event_bus(thresholds, monkeypatch):
    from hub import events
    subscribed = []
    monkeypatch.setattr(events, 'subscribe', subscribed.append)
    notifier.setup_alert_notify()
    assert len(subscribed) == 1 and callable(subscribed[0])
