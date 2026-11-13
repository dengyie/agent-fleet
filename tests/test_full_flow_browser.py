"""Browser → cookie auth → scheduler → real model HTTP → tools → artifact."""
import os
import subprocess

import pytest
from support.full_flow import FullFlowHub, ROOT
from support.strict_provider import StrictProvider, REPORT


def test_browser_account_model_tool_artifact_and_failure_journey(tmp_path, monkeypatch):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('FLEET_PLAYWRIGHT_MODULE is required for full-flow browser acceptance')
    monkeypatch.setenv('FLEET_FULL_FLOW_SECRET', 'full-flow-provider-secret')
    with StrictProvider() as provider:
        with FullFlowHub(tmp_path, provider, browser=True) as hub:
            result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/full_flow_browser.cjs'), hub.origin],
                                    capture_output=True, text=True, timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
            assert (hub.workspace / 'flow.txt').read_text() == REPORT
            assert provider.errors == []
            assert len(provider.requests) == 6  # Five success steps; one failed call, never replayed.
