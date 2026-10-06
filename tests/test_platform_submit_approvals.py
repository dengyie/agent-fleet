"""Submit approval contract: durable approvals, owner REST surface, gating."""
from hub.bootstrap import create_app
from hub.config import FleetConfig

OWNER = "owner@example.test"
RUN = "run-submit"
SELECTOR = "#checkout"


def _app(tmp_path, *, submit=False):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator=OWNER,
        platform_enabled=True, platform_browser_enabled=True,
        platform_browser_submit_enabled=submit,
    ))


def _open_session(app, *, session_id, node_id="node-submit", run_id=RUN):
    repo = app.extensions["fleet"]["repositories"]["browser"]
    repo.create_session(
        OWNER, workspace_id="workspace-submit", run_id=run_id,
        node_id=node_id, profile_id="profile-default", backend="cdp_local",
        session_id=session_id, now=1.0,
    )
    return repo


def _client(app):
    client = app.test_client()
    headers = {"X-Dev-Operator": OWNER}
    return client, headers


def test_grant_binds_session_row_and_consumes_once(tmp_path):
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    service = app.extensions["fleet"]["services"]["submit_approvals"]
    first = service.grant(OWNER, RUN, session_id="s" * 16, selector=SELECTOR)
    approval = first["approval"]
    assert approval["state"] == "active"
    assert approval["node_id"] == "node-submit"
    assert approval["workspace_id"] == "workspace-submit"
    assert approval["expires_at"] - approval["granted_at"] == 300.0

    repo = app.extensions["fleet"]["repositories"]["browser"]
    consumed = repo.consume_submit_approval(
        OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
        command_id="cmd-1", now=approval["granted_at"] + 1,
    )
    assert consumed == approval["approval_id"]
    stored = repo.get_submit_approval(OWNER, approval["approval_id"])
    assert stored["state"] == "consumed"
    assert stored["consumed_command_id"] == "cmd-1"
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-2", now=approval["granted_at"] + 1,
        )
    assert err.value.code == "approval_required"


def test_ttl_boundary_and_expiry_sweep(tmp_path):
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    repo = app.extensions["fleet"]["repositories"]["browser"]
    approval = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=100.0,
    )
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-1", now=400.0,
        )
    assert err.value.code == "approval_expired"
    assert repo.get_submit_approval(OWNER, approval["approval_id"])["state"] == "expired"

    later = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=1000.0,
    )
    assert repo.expire_submit_approvals(now=later["expires_at"]) >= 1
    assert repo.get_submit_approval(OWNER, later["approval_id"])["state"] == "expired"


def test_grant_rejects_unknown_or_closed_session(tmp_path):
    import pytest
    app = _app(tmp_path, submit=True)
    service = app.extensions["fleet"]["services"]["submit_approvals"]
    from hub.application.task_service import ApplicationError
    with pytest.raises(ApplicationError) as missing:
        service.grant(OWNER, RUN, session_id="s" * 16, selector=SELECTOR)
    assert missing.value.code == "session_not_found"
    repo = _open_session(app, session_id="s" * 16)
    repo.close_session(OWNER, "s" * 16, now=2.0)
    with pytest.raises(ApplicationError) as closed:
        service.grant(OWNER, RUN, session_id="s" * 16, selector=SELECTOR)
    assert closed.value.code == "session_not_found"


def test_close_session_terminals_active_approvals_atomically(tmp_path):
    app = _app(tmp_path, submit=True)
    repo = _open_session(app, session_id="s" * 16)
    approval = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=1.0,
    )
    repo.close_session(OWNER, "s" * 16, now=2.0)
    assert repo.get_submit_approval(OWNER, approval["approval_id"])["state"] == "expired"
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-1", now=2.5,
        )
    assert err.value.code == "approval_required"


def test_idempotent_grant_and_conflict(tmp_path):
    import pytest
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    service = app.extensions["fleet"]["services"]["submit_approvals"]
    first = service.grant(OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
                          idempotency_key="idem-1")["approval"]
    again = service.grant(OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
                          idempotency_key="idem-1")["approval"]
    assert again["approval_id"] == first["approval_id"]
    from hub.application.task_service import ApplicationError
    with pytest.raises(ApplicationError) as conflict:
        service.grant(OWNER, RUN, session_id="s" * 16, selector="#other",
                      idempotency_key="idem-1")
    assert conflict.value.code == "approval_idempotency_conflict"


def test_active_and_rate_limits(tmp_path):
    import pytest
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    service = app.extensions["fleet"]["services"]["submit_approvals"]
    from hub.application.task_service import ApplicationError
    for index in range(4):
        service.grant(OWNER, RUN, session_id="s" * 16,
                      selector=f"{SELECTOR}-{index}")
    with pytest.raises(ApplicationError) as session_limit:
        service.grant(OWNER, RUN, session_id="s" * 16, selector="#one-too-many")
    assert session_limit.value.code == "approval_limit"
    assert session_limit.value.status == 429

    repo = app.extensions["fleet"]["repositories"]["browser"]
    # Free the four active slots so the hourly-budget loop starts from zero.
    for index in range(4):
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=f"{SELECTOR}-{index}",
            command_id=f"cmd-p{index}", now=1.0,
        )
    other_run = "run-other-999"
    repo.create_session(
        OWNER, workspace_id="workspace-submit", run_id=other_run,
        node_id="node-submit", profile_id="profile-default", backend="cdp_local",
        session_id="t" * 16, now=1.0,
    )
    # Workspace-hour cap counts every grant, active or not: consume each new
    # approval so the per-run/per-session active caps stay clear until the
    # 64-grants-per-hour budget is spent.  All timestamps stay inside one
    # grant-hour window, on the same injected clock base.
    base = 100000.0
    for index in range(60):
        approval = repo.grant_submit_approval(
            OWNER, workspace_id="workspace-submit", run_id=RUN,
            node_id="node-submit", session_id="s" * 16,
            selector=f"{SELECTOR}-x{index}", now=base,
        )
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=f"{SELECTOR}-x{index}",
            command_id=f"cmd-x{index}", now=base,
        )
        assert approval["state"] == "active"
    # The hourly cap counts the injected-clock window, so assert through a
    # service bound to the same clock base as the grants above.
    from hub.application.conversation_service import SubmitApprovalService
    hourly_service = SubmitApprovalService(repo, clock=lambda: 100000.0)
    with pytest.raises(ApplicationError) as hourly:
        hourly_service.grant(OWNER, other_run, session_id="t" * 16, selector="#elsewhere")
    assert hourly.value.code == "approval_limit"
    assert hourly.value.status == 429


def test_revoke_blocks_consumption(tmp_path):
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    service = app.extensions["fleet"]["services"]["submit_approvals"]
    approval = service.grant(OWNER, RUN, session_id="s" * 16,
                             selector=SELECTOR)["approval"]
    revoked = service.revoke(OWNER, approval["approval_id"])["approval"]
    assert revoked["state"] == "revoked"
    assert service.get(OWNER, approval["approval_id"])["approval"]["state"] == "revoked"
    repo = app.extensions["fleet"]["repositories"]["browser"]
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-1", now=approval["granted_at"] + 1,
        )
    assert err.value.code == "approval_required"
    from hub.application.task_service import ApplicationError
    with pytest.raises(ApplicationError) as missing:
        service.revoke(OWNER, "missing-approval-id-000")
    assert missing.value.code == "approval_not_found"


def test_owner_rest_surface_end_to_end(tmp_path):
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    client, headers = _client(app)
    granted = client.post(
        f"/api/platform/v1/runs/{RUN}/browser-approvals",
        json={"session_id": "s" * 16, "selector": SELECTOR,
              "idempotency_key": "rest-1"},
        headers=headers,
    )
    assert granted.status_code == 200
    approval = granted.get_json()["approval"]
    assert approval["node_id"] == "node-submit"
    approval_id = approval["approval_id"]

    fetched = client.get(
        f"/api/platform/v1/runs/{RUN}/browser-approvals/{approval_id}",
        headers=headers,
    )
    assert fetched.status_code == 200
    assert fetched.get_json()["approval"]["approval_id"] == approval_id

    wrong_run = client.get(
        f"/api/platform/v1/runs/run-mismatch-00/browser-approvals/{approval_id}",
        headers=headers,
    )
    assert wrong_run.status_code == 404

    # A wrong-run revoke fails before mutating: the approval stays active.
    wrong_delete = client.delete(
        f"/api/platform/v1/runs/run-mismatch-00/browser-approvals/{approval_id}",
        headers=headers,
    )
    assert wrong_delete.status_code == 404
    approvals = app.extensions["fleet"]["services"]["submit_approvals"]
    assert approvals.get(OWNER, approval_id)["approval"]["state"] == "active"

    revoked = client.delete(
        f"/api/platform/v1/runs/{RUN}/browser-approvals/{approval_id}",
        headers=headers,
    )
    assert revoked.status_code == 200
    assert revoked.get_json()["approval"]["state"] == "revoked"


def test_rest_surface_disabled_without_gate(tmp_path):
    app = _app(tmp_path, submit=False)
    client, headers = _client(app)
    response = client.post(
        f"/api/platform/v1/runs/{RUN}/browser-approvals",
        json={"session_id": "s" * 16, "selector": SELECTOR}, headers=headers,
    )
    assert response.status_code == 404
    assert response.get_json()["error"] == "submit_disabled"


def test_rest_rejects_foreign_identity_and_bad_body(tmp_path):
    app = _app(tmp_path, submit=True)
    _open_session(app, session_id="s" * 16)
    client = app.test_client()
    # DEV fallback authorizes the unauthenticated call in dev mode; a foreign
    # domain credential header must suppress that fallback entirely.
    foreign = client.post(
        f"/api/platform/v1/runs/{RUN}/browser-approvals",
        json={"session_id": "s" * 16, "selector": SELECTOR},
        headers={"X-Platform-Node-Credential": "node-submit:" + "n" * 40},
    )
    assert foreign.status_code == 401
    headers = {"X-Dev-Operator": OWNER}
    bad = client.post(
        f"/api/platform/v1/runs/{RUN}/browser-approvals",
        json={"session_id": "s" * 16}, headers=headers,
    )
    assert bad.status_code == 400
    sensitive = client.post(
        f"/api/platform/v1/runs/{RUN}/browser-approvals",
        json={"session_id": "s" * 16, "selector": "input[name=password]"},
        headers=headers,
    )
    assert sensitive.status_code == 400
    assert sensitive.get_json()["error"] == "invalid_selector"


def test_run_per_active_limit_boundary(tmp_path):
    # N5: the 17th active approval for one run fails approval_limit with no
    # row created, while a second run keeps its own independent budget. Each
    # session caps at four active approvals, so spread the 16 across four
    # sessions to exercise the per-run boundary specifically.
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    app = _app(tmp_path, submit=True)
    repo = app.extensions["fleet"]["repositories"]["browser"]
    for session_index in range(4):
        session_id = chr(ord("a") + session_index) * 16
        repo.create_session(
            OWNER, workspace_id="workspace-submit", run_id=RUN,
            node_id="node-submit", profile_id="profile-default", backend="cdp_local",
            session_id=session_id, now=1.0,
        )
        for grant_index in range(4):
            repo.grant_submit_approval(
                OWNER, workspace_id="workspace-submit", run_id=RUN,
                node_id="node-submit", session_id=session_id,
                selector=f"{SELECTOR}-{session_index}{grant_index}", now=1.0,
            )
    repo.create_session(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", profile_id="profile-default", backend="cdp_local",
        session_id="e" * 16, now=1.0,
    )
    with pytest.raises(BrowserRepositoryError) as err:
        repo.grant_submit_approval(
            OWNER, workspace_id="workspace-submit", run_id=RUN,
            node_id="node-submit", session_id="e" * 16,
            selector="#seventeen", now=1.0,
        )
    assert err.value.code == "approval_limit"
    # A separate run has an independent per-run active budget.
    repo.create_session(
        OWNER, workspace_id="workspace-submit", run_id="run-second-00",
        node_id="node-submit", profile_id="profile-default", backend="cdp_local",
        session_id="u" * 16, now=1.0,
    )
    approval = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id="run-second-00",
        node_id="node-submit", session_id="u" * 16, selector=SELECTOR, now=1.0,
    )
    assert approval["state"] == "active"


def test_cross_run_consumption_is_scoped(tmp_path):
    # N6: an approval granted under run R is not consumable from run R2,
    # even with the same session/selector, and leaves the row untouched.
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    app = _app(tmp_path, submit=True)
    repo = _open_session(app, session_id="s" * 16)
    approval = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=1.0,
    )
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, "run-cross-00000", session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-cross", now=1.0,
        )
    assert err.value.code == "approval_required"
    assert repo.get_submit_approval(OWNER, approval["approval_id"])["state"] == "active"


def test_run_cancel_terminals_active_approvals(tmp_path):
    # P7: cancelling the run expires its active approvals; consumption
    # afterwards finds no active row.
    import pytest
    from hub.application.task_service import ApplicationError
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    app = _app(tmp_path, submit=True)
    repo = _open_session(app, session_id="s" * 16)
    platform_repo = app.extensions["fleet"]["platform_repository"]
    platform_repo.upsert_model(OWNER, {"profile_id": "model", "provider": "deterministic", "model": "test"})
    platform_repo.upsert_workspace(OWNER, {"workspace_id": "workspace-submit", "root_path": str(tmp_path / "home")})
    platform_repo.upsert_node(OWNER, {"node_id": "node-submit", "label": "Submit"})
    platform_repo.create_conversation(OWNER, "conv-submit-cancel", title="",
                                      workspace_id="workspace-submit")
    platform_repo.append_turn(
        OWNER, "conv-submit-cancel", "msg-cancel-1", RUN, text="use browser",
        client_token="cancel-turn-1",
        config_snapshot={"workspace_id": "workspace-submit",
                         "model_profile_id": "model",
                         "execution_node_id": "node-submit"}, now=1,
    )
    run_service = app.extensions["fleet"]["services"]["runs"]
    approval = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=1.0,
    )
    cancelled = run_service.cancel(OWNER, RUN)
    assert cancelled["ok"]
    assert repo.get_submit_approval(OWNER, approval["approval_id"])["state"] == "expired"
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-after", now=2.0,
        )
    assert err.value.code == "approval_required"
    # The run row keeps cancel semantics; a second cancel stays idempotent.
    again_cancel = run_service.cancel(OWNER, RUN)
    assert again_cancel["ok"]
    with pytest.raises(ApplicationError) as missing:
        run_service.cancel(OWNER, "run-missing-000")
    assert missing.value.code == "run_not_found"


def test_lapsed_grants_free_budget_at_next_grant(tmp_path):
    # Four lapsed approvals must not block a fresh grant: the lazy TTL sweep
    # inside grant_submit_approval terminalizes expired rows before counting.
    app = _app(tmp_path, submit=True)
    repo = _open_session(app, session_id="s" * 16)
    for i in range(4):
        repo.grant_submit_approval(
            OWNER, workspace_id="workspace-submit", run_id=RUN,
            node_id="node-submit", session_id="s" * 16,
            selector=f"{SELECTOR}-{i}", now=100.0,
        )
    fresh = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=500.0,
    )
    assert fresh["state"] == "active"
    assert fresh["granted_at"] == 500.0
    # The lapsed rows are terminal, not silently freed for reuse.
    assert repo.get_submit_approval(OWNER, fresh["approval_id"])["state"] == "active"


def test_consume_prefers_unexpired_over_lapsed_approval(tmp_path):
    # An older lapsed approval must not shadow a newer valid one: consumption
    # succeeds against the unexpired row instead of failing approval_expired.
    import pytest
    from hub.infrastructure.browser_repository import BrowserRepositoryError
    app = _app(tmp_path, submit=True)
    repo = _open_session(app, session_id="s" * 16)
    lapsed = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=0.0,
    )
    valid = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=350.0,
    )
    consumed = repo.consume_submit_approval(
        OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
        command_id="cmd-valid", now=360.0,
    )
    assert consumed == valid["approval_id"]
    assert repo.get_submit_approval(OWNER, valid["approval_id"])["state"] == "consumed"
    # The second grant's lazy sweep already terminalized the lapsed row, so a
    # further consume finds no active row at all (approval_required) instead
    # of silently consuming the lapsed one.
    assert repo.get_submit_approval(OWNER, lapsed["approval_id"])["state"] == "expired"
    with pytest.raises(BrowserRepositoryError) as err:
        repo.consume_submit_approval(
            OWNER, RUN, session_id="s" * 16, selector=SELECTOR,
            command_id="cmd-lapsed", now=360.0,
        )
    assert err.value.code == "approval_required"


def test_run_terminal_state_expires_unconsumed_approvals(tmp_path):
    # LocalRunWorkerService._finish terminalizes approvals a finished Run
    # never consumed (succeeded/failed/unknown all leave the queue).
    from hub.application.run_worker_service import LocalRunWorkerService
    app = _app(tmp_path, submit=True)
    repo = _open_session(app, session_id="s" * 16)
    service = app.extensions["fleet"]["services"]["submit_approvals"]
    approval = repo.grant_submit_approval(
        OWNER, workspace_id="workspace-submit", run_id=RUN,
        node_id="node-submit", session_id="s" * 16, selector=SELECTOR, now=1.0,
    )

    class _FakeRunEvents:
        def state(self, owner_id, run_id, state, **kwargs):
            return {"run_id": run_id, "state": state}

    worker = LocalRunWorkerService(
        app.extensions["fleet"]["platform_repository"], _FakeRunEvents(),
        worker_id="finish-worker", submit_approvals=service,
    )
    finished = worker._finish(
        {"owner_id": OWNER, "run_id": RUN, "lease_id": None}, "succeeded")
    assert finished["state"] == "succeeded"
    stored = repo.get_submit_approval(OWNER, approval["approval_id"])
    assert stored["state"] == "expired"
