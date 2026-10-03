"""Real socket poll/receipt fixture; only node placement is simulated."""
from threading import Event, Thread

from tools.platform.backends.directory import DirectoryBackend
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from tools.transport import Transport


class LoopbackNode:
    def __init__(self, hub):
        self.workspace = hub.root / 'node-workspace'
        self.workspace.mkdir()
        self.commands = []
        self.failures = []
        self.stopped = Event()
        repo = hub.app.extensions['fleet']['platform_repository']
        repo.upsert_node(hub.owner, {'node_id': 'loopback-node', 'label': 'Disposable node'})
        repo.provision_node_credential(hub.owner, 'loopback-node', secret='n' * 40)
        repo.upsert_workspace(hub.owner, {
            'workspace_id': 'remote', 'root_path': str(hub.workspace),
            'default_node_id': 'loopback-node',
        })
        self.journal = NodeJournal(hub.root / 'node-journal.db')
        self.journal.init()
        executor = NodeToolExecutor(DirectoryBackend(self.workspace))

        def execute(command):
            self.commands.append(command)
            return executor(command)

        self.client = NodeClient(
            self.journal, executor=execute, node_id='loopback-node',
            credential='loopback-node:' + 'n' * 40, hub_url=hub.origin,
            transport=Transport(mode='direct', attempts=1, timeout=2),
        )

        def poll():
            try:
                while not self.stopped.is_set():
                    result = self.client.poll_once()
                    if not result['ok'] or result['commands'] != result.get('receipts', 0):
                        self.failures.append(result)
                    self.stopped.wait(.05)
            except Exception as exc:
                self.failures.append(repr(exc))

        self.thread = Thread(target=poll, name='test-loopback-node', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(5)
        assert not self.thread.is_alive()
        assert not self.failures, self.failures
