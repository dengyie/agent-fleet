"""Explicit runtime configuration for the hub process."""

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class FleetConfig:
    """Resolved paths and runtime options shared by hub components.

    Path derivation is deliberately side-effect free. Repositories and the
    application bootstrap own directory creation and credential loading.
    """

    root: Path
    state_dir: Path
    hosts_file: Path
    event_log: Path
    task_db: Path
    ingest_token: str | None = None
    dev_operator: str | None = None
    runner_credentials: Mapping[str, str] | None = None
    project_whitelist: Mapping[str, Any] | None = None
    tasks_enabled: bool = True
    serve_frontend: bool = True
    frontend_dir: Path | None = None
    session_repositories_enabled: bool = False
    session_db: Path | None = None
    session_transcript_root: Path | None = None
    # Optional restricted transcript encryption source (additive). Exactly one
    # of these is consulted by ``tools.session.crypto.load_encryption_key`` at
    # bootstrap time and passed to the transcript repository constructor.  Key
    # material never appears in logs/errors/reports.  ``None`` = fail closed:
    # raw transcript is never written and never returned.
    session_encryption_raw: bytes | None = None
    session_encryption_key_src: Mapping[str, Any] | None = None
    # ---- additive supervisor control-plane settings (Task 9) ----
    # NOT reordered/renamed above; these are purely additive fields appended at
    # the end so no existing position/meaning changes.
    supervisor_enabled: bool = False
    supervisor_credentials: Mapping[str, str] | None = None
    supervisor_signing_raw: bytes | None = None
    supervisor_signing_key_src: Mapping[str, Any] | None = None
    supervisor_ttl_s: float = 3600.0
    # ---- additive adoption-control-plane settings (Task 4) ----
    # NOT reordered/renamed above; purely additive fields appended at the end
    # so no existing position/meaning changes. Disabled (default) keeps
    # ``adoption_db`` None so no adoption store is ever created implicitly.
    adoption_repositories_enabled: bool = False
    adoption_db: Path | None = None
    # ---- additive Phase 4/5 issuance gates (default off) ----
    # append_user_turn / apply_local_profile stay unsigned until explicitly
    # enabled.  Disabled keeps today's five-action operator surface.
    append_user_turn_enabled: bool = False
    apply_local_profile_enabled: bool = False
    # Additive platform domain gate.  Disabled by default so no platform DB is
    # created and no new routes are exposed during the existing rollout.
    platform_enabled: bool = False
    platform_db: Path | None = None
    platform_artifact_root: Path | None = None
    service_monitoring_enabled: bool = False
    # Controlled service actions are a separate fail-closed gate.  Monitoring
    # never implies that a node may mutate a service.
    service_actions_enabled: bool = False
    # Execution-window control plane is separate from service monitoring and
    # actions. It exposes only durable attach/lease metadata; process attach
    # remains intentionally unavailable in this slice.
    execution_windows_enabled: bool = False
    # Komari is an optional read-only observation source. No client is
    # constructed and no network request is made while this gate is false.
    komari_enabled: bool = False
    komari_network_enabled: bool = False
    komari_base_url: str | None = None
    komari_nodes_path: str | None = None
    komari_token: str | None = field(default=None, repr=False)
    komari_node_mapping: Mapping[str, str] | None = None
    # Background Komari synchronization is separate from the on-demand sync
    # route. It stays off until a deployment has an explicit scheduler plan.
    komari_sync_enabled: bool = False
    komari_sync_interval_s: float = 60.0
    # HTTP probe is a separate source and scheduler.  Every gate defaults off;
    # network access is never implied by a service definition.
    http_probe_enabled: bool = False
    http_probe_network_enabled: bool = False
    http_probe_sync_enabled: bool = False
    http_probe_sync_interval_s: float = 60.0
    http_probe_allowed_origins: tuple[str, ...] = ()
    http_probe_allow_loopback: bool = False
    incident_recovery_required: int = 2
    # Platform command signing is opt-in during the staged rollout.  When a
    # key is configured, the Hub signs unsigned commands before persistence;
    # strict mode rejects enqueue/poll of commands without a valid signature.
    platform_command_signing_raw: bytes | None = None
    platform_require_command_signature: bool = False
    # Local assistant Run worker is opt-in. Construction never starts a
    # thread; the process entrypoint must call ``start_platform_worker``.
    platform_worker_enabled: bool = False
    platform_worker_scheduler_enabled: bool = False
    platform_worker_max_concurrency: int = 1
    platform_worker_max_workspace_concurrency: int = 1
    platform_worker_interval_s: float = 1.0
    platform_worker_lease_s: float = 60.0
    platform_sandbox_launcher: tuple[str, ...] = ()
    # Real model network access is a separate rollout gate.  A provider
    # profile alone never causes the Hub to contact an endpoint.
    platform_provider_network_enabled: bool = False
    # Remote Run-to-Node tool delivery is a separate fail-closed rollout gate.
    platform_remote_execution_enabled: bool = False
    # Usage telemetry is available with the platform store; admission is
    # opt-in during the staged rollout.
    platform_usage_limits_enabled: bool = False
    # Durable read-only schedules are independent from Run and probe
    # schedulers. Disabled by default so app construction has no automation.
    platform_schedules_enabled: bool = False
    # Explicit MemoryItems are a separate default-off data plane. They are
    # never populated from model output implicitly.
    platform_memory_enabled: bool = False
    # Memory context injection is a second, independent default-off gate. CRUD
    # access never implies that a model Run may receive MemoryItems.
    platform_memory_context_enabled: bool = False
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_root(
        cls,
        root: Path,
        *,
        state_dir: Path | None = None,
        hosts_file: Path | None = None,
        event_log: Path | None = None,
        task_db: Path | None = None,
        ingest_token: str | None = None,
        dev_operator: str | None = None,
        runner_credentials: Mapping[str, str] | None = None,
        project_whitelist: Mapping[str, Any] | None = None,
        tasks_enabled: bool = True,
        serve_frontend: bool = True,
        frontend_dir: Path | None = None,
        session_repositories_enabled: bool = False,
        session_db: Path | None = None,
        session_transcript_root: Path | None = None,
        session_encryption_raw: bytes | None = None,
        session_encryption_key_src: Mapping[str, Any] | None = None,
        supervisor_enabled: bool = False,
        supervisor_credentials: Mapping[str, str] | None = None,
        supervisor_signing_raw: bytes | None = None,
        supervisor_signing_key_src: Mapping[str, Any] | None = None,
        supervisor_ttl_s: float = 3600.0,
        adoption_repositories_enabled: bool = False,
        adoption_db: Path | None = None,
        append_user_turn_enabled: bool = False,
        apply_local_profile_enabled: bool = False,
        platform_enabled: bool = False,
        platform_db: Path | None = None,
        platform_artifact_root: Path | None = None,
        service_monitoring_enabled: bool = False,
        platform_schedules_enabled: bool = False,
        service_actions_enabled: bool = False,
        execution_windows_enabled: bool = False,
        komari_enabled: bool = False,
        komari_network_enabled: bool = False,
        komari_base_url: str | None = None,
        komari_nodes_path: str | None = None,
        komari_token: str | None = None,
        komari_node_mapping: Mapping[str, str] | None = None,
        komari_sync_enabled: bool = False,
        komari_sync_interval_s: float = 60.0,
        http_probe_enabled: bool = False,
        http_probe_network_enabled: bool = False,
        http_probe_sync_enabled: bool = False,
        http_probe_sync_interval_s: float = 60.0,
        http_probe_allowed_origins: tuple[str, ...] | list[str] | None = None,
        http_probe_allow_loopback: bool = False,
        incident_recovery_required: int = 2,
        platform_command_signing_raw: bytes | None = None,
        platform_require_command_signature: bool = False,
        platform_worker_enabled: bool = False,
        platform_worker_scheduler_enabled: bool = False,
        platform_worker_max_concurrency: int = 1,
        platform_worker_max_workspace_concurrency: int = 1,
        platform_worker_interval_s: float = 1.0,
        platform_worker_lease_s: float = 60.0,
        platform_sandbox_launcher: tuple[str, ...] = (),
        platform_provider_network_enabled: bool = False,
        platform_remote_execution_enabled: bool = False,
        platform_usage_limits_enabled: bool = False,
        platform_memory_enabled: bool = False,
        platform_memory_context_enabled: bool = False,
        **extra: Any,
    ) -> "FleetConfig":
        root = Path(root).expanduser()
        state = Path(state_dir).expanduser() if state_dir is not None else root / "state"
        # Session repositories are an independent durable store that must never
        # live inside the legacy observation state tree for isolation.  They are
        # only derived when explicitly enabled; disabled (the default) keeps
        # both paths None so no session store is ever created implicitly.
        if session_repositories_enabled:
            session_db_path = (
                Path(session_db).expanduser()
                if session_db is not None
                else root / "var" / "sessions" / "meta.db"
            )
            session_transcript_root_path = (
                Path(session_transcript_root).expanduser()
                if session_transcript_root is not None
                else root / "var" / "sessions" / "transcripts"
            )
        else:
            session_db_path = None
            session_transcript_root_path = None
        # Adoption records are another independent durable store that must
        # never live inside the legacy observation state tree.  They are only
        # derived when explicitly enabled; disabled (the default) keeps
        # ``adoption_db`` None so no adoption store is ever created implicitly.
        if adoption_repositories_enabled:
            adoption_db_path = (
                Path(adoption_db).expanduser()
                if adoption_db is not None
                else root / "var" / "adoptions" / "meta.db"
            )
        else:
            adoption_db_path = None
        platform_db_path = (
            Path(platform_db).expanduser()
            if platform_db is not None
            else root / "var" / "platform" / "platform.db"
        ) if platform_enabled else None
        platform_artifact_root_path = (
            Path(platform_artifact_root).expanduser()
            if platform_artifact_root is not None
            else root / "var" / "platform" / "artifacts"
        ) if platform_enabled else None
        try:
            sync_interval = float(komari_sync_interval_s or 60.0)
        except (TypeError, ValueError):
            sync_interval = 60.0
        if not math.isfinite(sync_interval):
            sync_interval = 60.0
        sync_interval = max(5.0, min(3600.0, sync_interval))
        try:
            http_probe_interval = float(http_probe_sync_interval_s or 60.0)
        except (TypeError, ValueError):
            http_probe_interval = 60.0
        if not math.isfinite(http_probe_interval):
            http_probe_interval = 60.0
        http_probe_interval = max(5.0, min(3600.0, http_probe_interval))
        from hub.integrations.http_probe import canonical_probe_origin
        allowed_probe_origins_list = []
        for item in (http_probe_allowed_origins or ()):
            if not isinstance(item, str) or not item.strip():
                continue
            try:
                allowed_probe_origins_list.append(canonical_probe_origin(item))
            except Exception:
                continue
        allowed_probe_origins = tuple(sorted(set(allowed_probe_origins_list)))
        try:
            worker_interval = float(platform_worker_interval_s or 1.0)
        except (TypeError, ValueError):
            worker_interval = 1.0
        if not math.isfinite(worker_interval):
            worker_interval = 1.0
        worker_interval = max(0.1, min(60.0, worker_interval))
        try:
            worker_lease = float(platform_worker_lease_s or 60.0)
        except (TypeError, ValueError):
            worker_lease = 60.0
        if not math.isfinite(worker_lease):
            worker_lease = 60.0
        worker_lease = max(5.0, min(3600.0, worker_lease))
        try:
            worker_max_concurrency = max(1, min(64, int(platform_worker_max_concurrency)))
        except (TypeError, ValueError):
            worker_max_concurrency = 1
        try:
            worker_max_workspace_concurrency = max(
                1, min(64, int(platform_worker_max_workspace_concurrency)))
        except (TypeError, ValueError):
            worker_max_workspace_concurrency = 1
        if (not isinstance(platform_sandbox_launcher, (tuple, list))
                or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg
                       for arg in platform_sandbox_launcher)):
            raise ValueError("platform_sandbox_launcher must be an argv list")
        return cls(
            root=root,
            state_dir=state,
            hosts_file=Path(hosts_file).expanduser() if hosts_file is not None else root / "hosts.yaml",
            event_log=Path(event_log).expanduser() if event_log is not None else state / "events.jsonl",
            task_db=Path(task_db).expanduser() if task_db is not None else state / "fleet.db",
            ingest_token=ingest_token,
            dev_operator=dev_operator,
            runner_credentials=runner_credentials,
            project_whitelist=project_whitelist,
            tasks_enabled=bool(tasks_enabled),
            serve_frontend=bool(serve_frontend),
            frontend_dir=Path(frontend_dir).expanduser()
            if frontend_dir is not None else root / "frontend",
            session_repositories_enabled=bool(session_repositories_enabled),
            session_db=session_db_path,
            session_transcript_root=session_transcript_root_path,
            session_encryption_raw=session_encryption_raw,
            session_encryption_key_src=session_encryption_key_src,
            supervisor_enabled=bool(supervisor_enabled),
            supervisor_credentials=supervisor_credentials,
            supervisor_signing_raw=supervisor_signing_raw,
            supervisor_signing_key_src=supervisor_signing_key_src,
            supervisor_ttl_s=float(supervisor_ttl_s or 3600.0),
            adoption_repositories_enabled=bool(adoption_repositories_enabled),
            adoption_db=adoption_db_path,
            append_user_turn_enabled=bool(append_user_turn_enabled),
            apply_local_profile_enabled=bool(apply_local_profile_enabled),
            platform_enabled=bool(platform_enabled),
            platform_db=platform_db_path,
            platform_artifact_root=platform_artifact_root_path,
            service_monitoring_enabled=bool(service_monitoring_enabled),
            platform_schedules_enabled=bool(
                platform_schedules_enabled and platform_enabled),
            service_actions_enabled=bool(service_actions_enabled),
            execution_windows_enabled=bool(
                execution_windows_enabled and platform_enabled),
            komari_enabled=bool(komari_enabled),
            komari_network_enabled=bool(komari_network_enabled and komari_enabled and platform_enabled),
            komari_base_url=str(komari_base_url) if komari_base_url is not None else None,
            komari_nodes_path=str(komari_nodes_path) if komari_nodes_path is not None else None,
            komari_token=str(komari_token) if komari_token is not None else None,
            komari_node_mapping=dict(komari_node_mapping or {}),
            komari_sync_enabled=bool(komari_sync_enabled),
            komari_sync_interval_s=sync_interval,
            http_probe_enabled=bool(
                http_probe_enabled and platform_enabled and service_monitoring_enabled,
            ),
            http_probe_network_enabled=bool(
                http_probe_network_enabled and platform_enabled and service_monitoring_enabled
                and http_probe_enabled,
            ),
            http_probe_sync_enabled=bool(
                http_probe_sync_enabled and platform_enabled and service_monitoring_enabled
                and http_probe_enabled and http_probe_network_enabled,
            ),
            http_probe_sync_interval_s=http_probe_interval,
            http_probe_allowed_origins=allowed_probe_origins,
            http_probe_allow_loopback=bool(
                http_probe_allow_loopback and http_probe_enabled
                and platform_enabled and service_monitoring_enabled,
            ),
            incident_recovery_required=max(1, min(10, int(incident_recovery_required))),
            platform_command_signing_raw=platform_command_signing_raw,
            platform_require_command_signature=bool(platform_require_command_signature),
            platform_worker_enabled=bool(platform_worker_enabled and platform_enabled),
            platform_worker_scheduler_enabled=bool(
                platform_worker_scheduler_enabled and platform_enabled and platform_worker_enabled),
            platform_worker_max_concurrency=worker_max_concurrency,
            platform_worker_max_workspace_concurrency=worker_max_workspace_concurrency,
            platform_worker_interval_s=worker_interval,
            platform_worker_lease_s=worker_lease,
            platform_sandbox_launcher=tuple(platform_sandbox_launcher),
            platform_provider_network_enabled=bool(
                platform_provider_network_enabled and platform_enabled),
            platform_remote_execution_enabled=bool(
                platform_remote_execution_enabled and platform_enabled),
            platform_usage_limits_enabled=bool(
                platform_usage_limits_enabled and platform_enabled),
            platform_memory_enabled=bool(
                platform_memory_enabled and platform_enabled),
            platform_memory_context_enabled=bool(
                platform_memory_context_enabled and platform_enabled
                and platform_memory_enabled),
            extra=dict(extra),
        )
