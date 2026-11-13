"""Explicit local capacity probe; never runs as part of pytest collection.

Run from the checkout with PYTHONPATH=.:tests and a fresh evidence directory.
Uses disposable fixture identities, real SQLite/HTTP and no external service.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

import psutil


def distribution(values):
    ordered = sorted(values)
    return {"count": len(values), "p50_ms": ordered[math.ceil(len(values) * .5) - 1],
            "p95_ms": ordered[math.ceil(len(values) * .95) - 1], "max_ms": ordered[-1],
            "samples_ms": values}


@contextmanager
def measure_process(report):
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = [baseline]
    stopped = threading.Event()
    def sample():
        while not stopped.wait(.01):
            peak[0] = max(peak[0], process.memory_info().rss)
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    waits = []
    real_connect = sqlite3.connect
    class TimedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            started = time.perf_counter()
            try:
                return super().execute(sql, *args, **kwargs)
            finally:
                if sql.strip().upper().startswith(("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")):
                    waits.append((time.perf_counter() - started) * 1000)
    def connect(*args, **kwargs):
        kwargs.setdefault("factory", TimedConnection)
        return real_connect(*args, **kwargs)
    sqlite3.connect = connect
    try:
        yield
    finally:
        sqlite3.connect = real_connect
        peak[0] = max(peak[0], process.memory_info().rss)
        stopped.set()
        thread.join(2)
        report["rss"] = {"baseline_bytes": baseline, "peak_bytes": peak[0],
                         "peak_increment_bytes": peak[0] - baseline, "sample_interval_ms": 10}
        report["begin_write_elapsed"] = distribution(waits) if waits else None
        report["begin_write_measurement"] = "BEGIN IMMEDIATE/EXCLUSIVE elapsed, including acquisition and Python overhead"


def read_probe(root):
    from test_workflow_http import http_setup
    app, client, setup = http_setup(root, count=1000)
    # Populate a full event page through the repository's transaction writer.
    with setup.repository.transaction() as conn:
        for sequence in range(2, 102):
            conn.execute("INSERT INTO workflow_events(owner_id,workspace_id,workflow_id,sequence,kind,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                         ("owner@example.test", "workspace-a", "workflow-000", sequence, "probe.event", b"{}", 100))
    report = {"workflows": 1000, "event_page_size": 100, "transport": "Flask WSGI test client, serial, no TCP"}
    paths = {"list_100": "/api/platform/v1/workflows?limit=100",
             "detail": "/api/platform/v1/workflows/workflow-000",
             "events_100": "/api/platform/v1/workflows/workflow-000/events?after=0&limit=100"}
    with measure_process(report):
        for name, path in paths.items():
            samples = []
            for index in range(55):
                started = time.perf_counter()
                response = client.get(path)
                assert response.status_code == 200, response.get_json()
                payload = response.get_json()
                if name == "list_100":
                    assert len(payload["workflows"]) == 100
                if name == "events_100":
                    assert len(payload["events"]) == 100
                if index >= 5:
                    samples.append((time.perf_counter() - started) * 1000)
            report[name] = distribution(samples)
            report[name]["response_bytes"] = len(response.data)
    return report


def coordinator_probe(root):
    from test_workflow_coordinator import make_setup
    setup = make_setup(root, count=32)
    coordinator = setup.coordinator()
    report = {"active_workflows": 32, "workflow_limit": 32, "action_limit": 64,
              "external_model_calls": 0, "execution": "real durable Runs; no CLI or model completion"}
    samples = []
    actions = []
    with measure_process(report):
        for index in range(35):
            started = time.perf_counter()
            result = setup.tick(coordinator)
            assert not result.failures
            if index >= 5:
                samples.append((time.perf_counter() - started) * 1000)
                actions.append(result.action_budget_used)
    report["tick"] = distribution(samples)
    report["actions_per_tick"] = actions
    report["created_executions"] = setup.executor.created
    with setup.repository.transaction(write=False) as conn:
        report["durable_runs"] = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    assert report["created_executions"] == report["durable_runs"] == 32
    return report


def transfer_probe(root, file_mib):
    from fleet_core.control import generate_ed25519_keypair
    from fleet_core.platform_command import PlatformCommand
    from hub.application.workflow_dispatch import WorkflowDispatchService
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from hub.domain.workflow_contract import parse_definition, resolve_contract
    from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef
    from hub.infrastructure.command_repository import CommandRepository
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_run_executor import LegacyTaskTarget, RegisteredRunTarget, RunStepExecutor
    from support.artifact_http import server
    from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert shutil.disk_usage(root).free > file_mib * 1024 * 1024 * 10, "insufficient scratch capacity"
    repository = prepare_database(root / "platform.db")
    db = PlatformRepository(repository.db_path)
    private, public = generate_ed25519_keypair()
    raw = json.loads(registered().snapshot)
    contract = raw["steps"][0]["contract"]
    contract["output_specs"][0]["max_bytes"] = 256 * 1024 * 1024
    contract["budget"].update(max_active_seconds=7200, max_wait_seconds=7200)
    definition = parse_definition(json.dumps(raw).encode())
    commands = CommandRepository(repository.db_path, clock=lambda: 104)
    commands.init()
    envelopes = []
    for index in range(4):
        node = "node-a" if index < 2 else "node-b"
        workflow, conversation = f"workflow-{index}", f"execution-{index}"
        db.create_conversation(OWNER, conversation, title="Capacity probe", workspace_id=WORKSPACE)
        repository.create_or_get(OWNER, WORKSPACE, workflow, client_token=workflow, definition=definition, now=100)
        claim = repository.claim_due(OWNER, worker_id=f"coordinator-{index}", now=100, limit=1, lease_seconds=60)[0]
        resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
        ready = repository.freeze_inputs(claim, step_key="develop", generation=1, resolved=resolved, expected_revision=1, now=101)
        repository.admit_step(claim, step_key="develop", generation=1, expected_revision=ready.revision,
                              target_key=node, capacity=2, reserve_tokens=100, reserve_requests=2, now=102)
        target = RegisteredRunTarget(OWNER, WORKSPACE, node, CatalogRef("cli", 1), CatalogRef("safe", 1),
                                     conversation, b"{}", LegacyTaskTarget("mac-local", "codex", "agent-fleet"))
        executor = RunStepExecutor(repository, target, clock=lambda: 104)
        observation = asyncio.run(WorkflowDispatchService(repository, executor, clock=lambda: 104).dispatch(
            claim, step_key="develop", generation=1,
            expected_revision=repository.get(OWNER, workflow).revision))
        command = PlatformCommand.create(command_id=f"export-{index}", target_node=node, owner_id=OWNER,
            action="artifact.export", resource_id=WORKSPACE, arguments={"output_key": "report"},
            expires_at=3700, run_id=observation.binding.run_id).signed(private)
        commands.enqueue(command)
        envelopes.append((node, command.command_id))
    for node in ("node-a", "node-b"):
        assert len(commands.claim_for_node(node, "node-worker", owner_id=OWNER, limit=2, lease_s=3600)) == 2
    app = create_app(FleetConfig.from_root(root, platform_enabled=True, platform_db=repository.db_path,
        platform_artifact_root=root / "artifacts", platform_workflows_enabled=True,
        platform_workflow_dispatch_enabled=True, platform_artifact_transfer_enabled=True,
        platform_command_signing_raw=private, dev_operator=OWNER, serve_frontend=False),
        repositories={"workflow_clock": lambda: 104})
    credentials = {}
    for node in ("node-a", "node-b"):
        db.upsert_node(OWNER, {"node_id": node, "label": node})
        credentials[node] = db.provision_node_credential(OWNER, node, secret=node[-1] * 40)["credential"]
    chunk = bytes(range(256)) * 4096
    chunk_hash = hashlib.sha256(chunk).hexdigest()
    content_hash = hashlib.sha256()
    for _ in range(file_mib):
        content_hash.update(chunk)
    digest, size = content_hash.hexdigest(), len(chunk) * file_mib
    report = {"file_bytes": size, "streams": 4, "nodes": 2, "per_node_streams": 2,
              "chunk_bytes": len(chunk), "transport": "real loopback HTTP PUT + commit; Hub and clients in one process",
              "sha256": digest, "clock": "fixed authorization clock 104; durations use monotonic real time"}
    with server(app) as url:
        # Reserve sequentially so the throughput measurement starts with all
        # four admitted streams; admission races have separate regression tests.
        reservations = []
        def request(opener, method, path, headers, body=None):
            with opener.open(Request(url + path, data=body, headers=headers, method=method), timeout=90) as response:
                return json.load(response)
        for node, command_id in envelopes:
            headers = {"X-Platform-Node-Credential": credentials[node], "X-Platform-Command-ID": command_id,
                       "X-Platform-Worker-ID": "node-worker", "Content-Type": "application/json"}
            opener = build_opener(ProxyHandler({}))
            response = request(opener, "POST", "/api/platform/v1/node/artifact-transfers", headers,
                json.dumps({"command_id": command_id, "output_key": "report", "name": "report.bin", "size": size, "sha256": digest}).encode())
            reservations.append((opener, headers, response["transfer"]))
        barrier = threading.Barrier(4)
        def upload(item):
            opener, headers, reservation = item
            path = "/api/platform/v1/node/artifact-transfers/" + reservation["transfer_id"]
            latencies = []
            barrier.wait(timeout=10)
            started = time.perf_counter()
            for offset in range(0, size, len(chunk)):
                before = time.perf_counter()
                part = request(opener, "PUT", path + "/parts/" + str(offset),
                    headers | {"Content-Type": "application/octet-stream", "X-Content-SHA256": chunk_hash}, chunk)
                assert part["part"]["sha256"] == chunk_hash
                latencies.append((time.perf_counter() - before) * 1000)
            artifact = request(opener, "POST", path + "/commit", headers,
                json.dumps({"command_id": headers["X-Platform-Command-ID"], "size": size, "sha256": digest}).encode())["artifact"]
            assert artifact["sha256"] == digest and artifact["size"] == size
            return {"seconds": time.perf_counter() - started, "parts": distribution(latencies), "artifact_id": artifact["artifact_id"]}
        with measure_process(report):
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=4) as pool:
                completed = list(pool.map(upload, reservations))
            elapsed = time.perf_counter() - started
        report.update(seconds=elapsed, aggregate_mib_per_second=4 * file_mib / elapsed, completed=completed)
        for item in completed:
            path = root / "artifacts" / item["artifact_id"] / "content"
            check = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(65536), b""):
                    check.update(block)
            assert path.stat().st_size == size and check.hexdigest() == digest
        report["independent_file_hashes_verified"] = 4
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("reads", "coordinator", "transfer"))
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--file-mib", type=int, choices=(8, 64), default=64)
    args = parser.parse_args()
    args.evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    repo = Path.cwd()
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=repo,
    ).decode("utf-8").split("\0")
    sources = {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
               for name in sorted(set(names) - {""}) if (repo / name).is_file()}
    source_manifest = json.dumps(sources, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    (args.evidence / "source-manifest.json").write_text(source_manifest, encoding="utf-8")
    report = {"schema_version": 1, "phase": args.phase, "python": sys.version,
              "platform": platform.platform(), "cpu_count": os.cpu_count(),
              "cpu_model": subprocess.check_output(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
                           if sys.platform == "darwin" else platform.processor(),
              "physical_memory_bytes": psutil.virtual_memory().total,
              "psutil_version": psutil.__version__,
              "sqlite_version": sqlite3.sqlite_version,
              "load_average_before": list(os.getloadavg()),
              "disk": dict(shutil.disk_usage(args.evidence)._asdict()),
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
              "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repo)),
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_manifest_sha256": hashlib.sha256(source_manifest.encode()).hexdigest(),
              "scope": "isolated local fixture capacity sample; no external task, model, deployment or capacity guarantee"}
    try:
        report["result"] = {"reads": read_probe, "coordinator": coordinator_probe,
                            "transfer": lambda root: transfer_probe(root, args.file_mib)}[args.phase](args.evidence / "fixture")
        assert all((repo / name).is_file() and hashlib.sha256((repo / name).read_bytes()).hexdigest() == digest
                   for name, digest in sources.items()), "source changed during measurement"
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        report["load_average_after"] = list(os.getloadavg())
        (args.evidence / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
