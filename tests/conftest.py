import pytest

from support.hub_env import HubStores

pytest_plugins = ['tools.testing.pytest_journeys']


@pytest.fixture
def hub_stores(tmp_path):
    """Observation/task/event stores on ``tmp_path`` (see ``support.hub_env``)."""
    return HubStores(tmp_path)
