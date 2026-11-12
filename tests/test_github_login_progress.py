"""Device authorization becomes ready only after both CLI prompt fields arrive."""
import threading

import pytest

from tools.plugins import login_worker
from tools.plugins.login_store import LoginStore


CODE = '! First copy your one-time code: ABCD-1234'
URL = 'Open this URL to continue in your web browser: https://github.com/login/device'


@pytest.mark.parametrize('lines', [(CODE, URL), (URL, CODE)])
def test_partial_prompt_does_not_publish_waiting_authorization(tmp_path, monkeypatch, lines):
    store = LoginStore(tmp_path / 'logins.db')
    store.create('login_test', 'default', 'github.com')
    parent_closed = threading.Event()
    observations = []

    class Parent:
        def read(self):
            parent_closed.wait()

    def stream(_argv, *, on_line, should_abort, **_kwargs):
        assert not should_abort()
        on_line(lines[0])
        observations.append(store.get('login_test'))
        on_line(lines[1])
        observations.append(store.get('login_test'))
        return 0

    monkeypatch.setattr(login_worker, 'run_streamed', stream)
    try:
        login_worker.run(store, 'login_test', 'unused', parent_stream=Parent())
    finally:
        parent_closed.set()
    partial, ready = observations
    assert partial['state'] == 'starting'
    field = 'user_code' if lines[0] == CODE else 'verification_uri'
    assert field in partial
    assert ready['state'] == 'waiting_authorization'
    assert ready['user_code'] == 'ABCD-1234'
    assert ready['verification_uri'] == 'https://github.com/login/device'
    assert store.get('login_test')['state'] == 'succeeded'
