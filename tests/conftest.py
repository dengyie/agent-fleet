import pytest

from support.hub_env import HubStores

pytest_plugins = ['tools.testing.pytest_journeys']


@pytest.fixture
def hub_stores(tmp_path):
    """Observation/task/event stores on ``tmp_path`` (see ``support.hub_env``)."""
    return HubStores(tmp_path)


@pytest.fixture(autouse=True)
def _isolated_client_home(tmp_path_factory, monkeypatch):
    """节点进程的启动回执 / drain 文件写到临时目录，绝不碰真实 ~/.local/share。"""
    monkeypatch.setenv("AGENT_FLEET_CLIENT_HOME",
                       str(tmp_path_factory.mktemp("client-home")))
