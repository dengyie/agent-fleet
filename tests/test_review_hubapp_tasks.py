"""评审修复 #19：目标机最近上报明确缺少该 agent 类型时拒绝建任务；未知放行。"""
import unittest

from hub.application.task_service import ApplicationError, TaskHostPolicy, TaskService


class _Obs:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def read_current(self, machine):
        return self.snapshot


class _Repo:
    def __init__(self):
        self.created = []

    def create_task(self, **kw):
        self.created.append(kw)
        return dict(kw, task_id="t1", state="queued"), True


class _Pub:
    def emit(self, *a, **kw):
        pass


def _service(agents, repo):
    snapshot = {"machine": "hk", "reachable": True}
    if agents is not ...:
        snapshot["agents"] = agents
    return TaskService(repo, _Obs(snapshot), _Pub(),
                       TaskHostPolicy(project_whitelist={"hk": ["agent-fleet"]}))


def _create(service, agent_type="codex"):
    return service.create({"machine": "hk", "agent_type": agent_type,
                           "project": "agent-fleet", "instruction": "run"}, "op")


class AgentAvailabilityTests(unittest.TestCase):
    def test_rejects_agent_not_reported(self):
        repo = _Repo()
        with self.assertRaises(ApplicationError) as cm:
            _create(_service({"claude_code": {"installed": True}}, repo))
        self.assertEqual(cm.exception.code, "agent_unavailable")
        self.assertEqual(repo.created, [])

    def test_rejects_agent_reported_not_installed(self):
        repo = _Repo()
        with self.assertRaises(ApplicationError) as cm:
            _create(_service({"codex": {"installed": False}}, repo))
        self.assertEqual(cm.exception.code, "agent_unavailable")
        self.assertEqual(repo.created, [])

    def test_unknown_allows(self):
        for agents in (..., {}, None, {"codex": {"error": "OSError"}}, {"codex": {}}):
            with self.subTest(agents=agents):
                repo = _Repo()
                _create(_service(agents, repo))
                self.assertEqual(len(repo.created), 1)

    def test_installed_agent_allows(self):
        repo = _Repo()
        _create(_service({"codex": {"installed": True}, "claude_code": {"installed": False}}, repo))
        self.assertEqual(repo.created[0]["agent_type"], "codex")


if __name__ == "__main__":
    unittest.main()
