"""Per-task process limits must not consume a shared host user's task budget."""
from __future__ import annotations

import json
import sys
import threading

import pytest

from hub.platform.workflow_validators import FixedTestRunner
from test_workflow_repository import OWNER, WORKSPACE
from test_workflow_sandbox import isolated_test_backend
from test_workflow_test_execution import ExecutionFixture


@pytest.mark.skipif(sys.platform != "linux", reason="Linux user namespace process accounting")
def test_fixed_test_process_budget_is_independent_of_unrelated_owner_threads(tmp_path):
    source = '''import errno,resource,subprocess,sys
def test_namespace_process_budget():
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (32,32)
    children=[]
    try:
        denied=False
        for _ in range(40):
            try:
                children.append(subprocess.Popen([sys.executable,"-c","import time; time.sleep(15)"]))
            except OSError as exc:
                assert exc.errno == errno.EAGAIN
                denied=True
                break
        assert denied and 1 <= len(children) < 32
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait(timeout=5)
'''
    fixture = ExecutionFixture(tmp_path, content=source)
    fixture.dispatch()
    stop = threading.Event()
    threads = [threading.Thread(target=stop.wait) for _ in range(80)]
    try:
        for thread in threads:
            thread.start()
        result = fixture.worker(runner=FixedTestRunner(backend_factory=isolated_test_backend)).run_once(OWNER)
    finally:
        stop.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
    assert result["state"] == "succeeded", result["result_text"]
    outputs = fixture.outputs()
    report = json.loads(fixture.store.read(OWNER, WORKSPACE, outputs["test_report"]["artifact_id"]))
    assert report["passed"] is True and report["exit_code"] == 0
    assert fixture.test_evidence()[0]["result"] == "passed"
    assert fixture.store.read(OWNER, WORKSPACE, outputs["junit"]["artifact_id"])
    assert all(not thread.is_alive() for thread in threads)
