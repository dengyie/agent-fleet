"""Project authoritative provider usage in the existing platform transaction."""
from __future__ import annotations

import sqlite3

from hub.domain.workflow_budget import BudgetTick, WorkflowUsage
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_json import decode_snapshot
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _integer, _time

_TOKENS = "CASE WHEN settlement='settled' THEN charged_tokens ELSE max(reserved_tokens,coalesce(charged_tokens,0)) END"
_REQUESTS = "CASE WHEN settlement='settled' THEN charged_requests ELSE max(reserved_requests,coalesce(charged_requests,0)) END"


def ledger_usage(connection: sqlite3.Connection, owner: str, run: str) -> WorkflowUsage | None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('platform_usage_events','platform_usage_reservations')")}
    if len(tables) != 2:
        return None
    columns = {row[1] for row in connection.execute("PRAGMA table_info(platform_usage_events)")}
    complete = "sum(summary_complete)" if "summary_complete" in columns else "0"
    row = connection.execute(
        f"SELECT count(*),coalesce(sum(total_tokens),0),coalesce({complete},0) FROM platform_usage_events WHERE owner_id=? AND run_id=?",
        (owner, run),
    ).fetchone()
    pending = connection.execute("SELECT 1 FROM platform_usage_reservations WHERE owner_id=? AND run_id=? LIMIT 1", (owner, run)).fetchone()
    if row[0] == 0:
        return None
    try:
        return WorkflowUsage(row[1], row[0], pending is None and row[2] == row[0])
    except WorkflowValidationError as exc:
        raise WorkflowRepositoryError("invalid_ledger_usage") from exc


def check_request_budget(connection: sqlite3.Connection, owner: str, run: str, estimated: int, now: float) -> str | None:
    """Return the exceeded dimension; ordinary Runs keep their existing policy."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_budget_reservations'").fetchone() is None:
        return None
    row = connection.execute(
        "SELECT q.owner_id,q.workflow_id,q.step_key,q.generation,s.contract_snapshot,s.is_current,s.state,"
        "s.active_deadline,s.wait_deadline,r.cancel_requested,r.pause_requested,m.admission_mode "
        "FROM workflow_budget_reservations q JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
        "JOIN workflow_runs r USING(owner_id,workspace_id,workflow_id) CROSS JOIN workflow_schema_meta m "
        "WHERE q.child_request_id=? AND m.singleton=1", (run,),
    ).fetchone()
    if row is None:
        return None
    if row["owner_id"] != owner or not row["is_current"] or row["cancel_requested"] or row["admission_mode"] == "frozen":
        return "usage"
    if row["state"] in ("unknown", "failed", "succeeded", "cancelled", "cancelling"):
        return "usage"
    deadline = row["wait_deadline"] if row["state"] in ("waiting_node", "waiting_external", "waiting_approval", "paused") else row["active_deadline"]
    if deadline is not None and now >= deadline:
        return "usage"
    budget = decode_snapshot(bytes(row["contract_snapshot"]))["budget"]
    prior = connection.execute(
        f"SELECT coalesce(sum({_TOKENS}),0),coalesce(sum({_REQUESTS}),0) FROM workflow_budget_reservations "
        "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation!=?",
        (owner, row["workflow_id"], row["step_key"], row["generation"]),
    ).fetchone()
    dirty_prior = connection.execute(
        "SELECT 1 FROM workflow_budget_reservations q JOIN workflow_budget_checks c USING(owner_id,workflow_id,step_key,generation) "
        "WHERE q.owner_id=? AND q.workflow_id=? AND q.step_key=? AND q.generation!=? AND c.active=1 AND q.settlement='settled' LIMIT 1",
        (owner, row["workflow_id"], row["step_key"], row["generation"]),
    ).fetchone()
    if dirty_prior is not None:
        return "usage"
    usage = ledger_usage(connection, owner, run)
    columns = {item[1] for item in connection.execute("PRAGMA table_info(platform_usage_events)")}
    if "summary_complete" not in columns:
        return "usage"
    incomplete = connection.execute("SELECT 1 FROM platform_usage_events WHERE owner_id=? AND run_id=? AND summary_complete!=1 LIMIT 1", (owner, run)).fetchone()
    if incomplete is not None:
        return "usage"
    pending = connection.execute("SELECT coalesce(sum(estimated_tokens),0),count(*) FROM platform_usage_reservations WHERE owner_id=? AND run_id=?",
                                 (owner, run)).fetchone()
    tokens = prior[0] + (usage.tokens if usage else 0) + pending[0]
    requests = prior[1] + (usage.requests if usage else 0) + pending[1]
    if requests + 1 > budget["max_model_requests"]:
        return "requests"
    if tokens >= budget["max_tokens"] or tokens + estimated > budget["max_tokens"]:
        return "tokens"
    return None


class WorkflowBudgetRepository:
    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository

    def _project(self, connection: sqlite3.Connection, owner: str, workflow: str, step: str, generation: int, now: float) -> bool:
        row = connection.execute(
            "SELECT q.settlement,q.charged_tokens,q.charged_requests,q.child_request_id,s.state FROM workflow_budget_reservations q "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "WHERE q.owner_id=? AND q.workflow_id=? AND q.step_key=? AND q.generation=?", (owner, workflow, step, generation),
        ).fetchone()
        if row is None or row["child_request_id"] is None:
            return False
        usage = ledger_usage(connection, owner, row["child_request_id"])
        terminal = row["state"] in ("succeeded", "failed", "cancelled")
        settlement = "settled" if terminal and usage is not None and usage.complete else ("unknown" if terminal or row["state"] == "unknown" else "reserved")
        if row["settlement"] == "settled" and (usage is None or usage.tokens < row["charged_tokens"] or usage.requests < row["charged_requests"]):
            raise WorkflowRepositoryError("usage_ledger_regression")
        values = (settlement, usage.tokens if usage else row["charged_tokens"], usage.requests if usage else row["charged_requests"])
        changed = tuple(row)[:3] != values
        if changed:
            connection.execute(
                "UPDATE workflow_budget_reservations SET settlement=?,charged_tokens=?,charged_requests=?,updated_at=? "
                "WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?", (*values, now, owner, workflow, step, generation),
            )
            connection.execute("UPDATE workflow_runs SET revision=revision+1,updated_at=? WHERE owner_id=? AND workflow_id=?", (now, owner, workflow))
            self.repository._event(connection, owner, workflow, "step.usage_projected",
                                   {"step_key": step, "generation": generation, "settlement": settlement,
                                    "tokens": values[1], "requests": values[2]}, now)
        if settlement == "settled":
            connection.execute("UPDATE workflow_budget_checks SET active=0 WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                               (owner, workflow, step, generation))
        return changed

    def project_due(self, *, now: float, limit: int = 32) -> BudgetTick:
        now = _time(now)
        _integer(limit, maximum=32)
        with self.repository.transaction() as connection:
            rows = connection.execute(
                "SELECT owner_id,workflow_id,step_key,generation FROM workflow_budget_checks WHERE active=1 "
                "ORDER BY service_sequence,owner_id,workflow_id,step_key,generation LIMIT ?", (limit,),
            ).fetchall()
            sequence = connection.execute("SELECT coalesce(max(service_sequence),0) FROM workflow_budget_checks").fetchone()[0]
            changed = 0
            for owner, workflow, step, generation in rows:
                changed += self._project(connection, owner, workflow, step, generation, now)
                sequence = _integer(sequence + 1)
                connection.execute(
                    "UPDATE workflow_budget_checks SET service_sequence=? WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=?",
                    (sequence, owner, workflow, step, generation),
                )
            return BudgetTick(len(rows), changed)
