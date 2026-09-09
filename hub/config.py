"""Explicit runtime configuration for the hub process."""

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
    frontend_cutover: bool = False
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
        frontend_cutover: bool = False,
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
            frontend_cutover=bool(frontend_cutover),
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
            extra=dict(extra),
        )
