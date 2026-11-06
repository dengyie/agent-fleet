"""Supervisor control plane: signing, task-cancel routing and adoption."""

from dataclasses import dataclass
from typing import Any


@dataclass
class ControlPlane:
    supervisor_service: Any
    control_router: Any = None
    adoption_service: Any = None


def build_control_plane(config, repositories: dict[str, Any], core,
                        sessions) -> ControlPlane:
    """Supervisor service, then the gated cancel router and adoption wiring.

    ``sessions.transcript_repo`` is filled in when adoption needs a transcript
    store that the session block did not build.
    """
    # Task 9: supervisor control-plane.  Construction is additive — when
    # ``supervisor_enabled`` is off, no route is registered, no durable command
    # store is opened and the service gets NO signing key (fail-closed issuance:
    # nothing could ever poll such a queue).  When on, the queue/audit always
    # live in the durable ``SupervisorCommandRepository``; the service has no
    # separate in-memory queue.  Signing key/credential VALUES are never logged
    # or printed; only their resolved source is noted.
    from hub.application.supervisor_service import SupervisorService
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository
    from hub.auth import load_supervisor_signing_key

    session_service = sessions.session_service
    session_repo = sessions.session_repo

    # Signing key resolution: explicit raw bytes > env/file.  ``None`` (no
    # key anywhere) is the fail-closed state — commands can never be issued.
    supervisor_key = None
    if config.supervisor_enabled:
        supervisor_key = (
            config.supervisor_signing_raw
            if config.supervisor_signing_raw is not None
            else load_supervisor_signing_key()
        )
    supervisor_command_repository = None
    if config.supervisor_enabled:
        command_db = (getattr(config, "supervisor_command_db", None)
                      or config.root / "var" / "supervisor" / "commands.db")
        supervisor_command_repository = SupervisorCommandRepository(command_db)
    supervisor_service = SupervisorService(
        signing_key=supervisor_key,
        ttl_s=float(config.supervisor_ttl_s or 3600.0),
        append_user_turn_enabled=bool(
            getattr(config, "append_user_turn_enabled", False)),
        apply_local_profile_enabled=bool(
            getattr(config, "apply_local_profile_enabled", False)),
        command_repository=supervisor_command_repository,
    )

    if session_service is not None and config.supervisor_enabled:
        session_service.supervisor = supervisor_service

    # Task 10: connect task cancel to managed-attempt termination.  Additive
    # and gated: the control router exists only when BOTH the supervisor plane
    # is enabled AND session repositories (managed-session metadata) are on.
    # When the gate is off, ``cancel_router`` stays ``None`` and the TaskService
    # cancel surface is byte-identical to the pre-Task-10 behavior (no command,
    # no audit).  The receipt hook only overrides the ``receipt`` transport;
    # every other supervisor contract is forwarded unchanged.  No key/secret
    # VALUE is ever stored/logged by this wiring.
    control_router = None
    if bool(config.supervisor_enabled
            and session_service is not None
            and session_repo is not None):
        from hub.application.control_router import (
            ControlRouter,
            SupervisorReceiptHook,
        )

        control_router = ControlRouter(
            supervisor=supervisor_service,
            task_repository=core.task_repository,
            session_repo=session_repo,
        )
        supervisor_service = SupervisorReceiptHook(
            supervisor_service, control_router, session_repo=session_repo)
        core.task_service.cancel_router = control_router

    # Task 6: operator adoption (纳管) wiring.  Additive and gated by
    # ``adoption_repositories_enabled``: when off, no adoption service and no
    # route exist and the old surface is byte-identical.  The adoption store is
    # exactly the Task 4 store; the transcript audit store reuses the session
    # block's repository when both systems are enabled, otherwise it is built
    # the same way (a missing transcript root fails loudly like the session
    # block, never silently using None).
    adoption_service = None
    if config.adoption_repositories_enabled:
        from hub.application.adoption_service import AdoptionService
        from hub.infrastructure.adoption_repository import AdoptionRepository

        adoption_repo = repositories.get("adoptions")
        if adoption_repo is None:
            if config.adoption_db is None:
                raise RuntimeError(
                    "adoption_repositories_enabled requires an adoption_db path")
            adoption_repo = AdoptionRepository(config.adoption_db)
            adoption_repo.init()
        if sessions.transcript_repo is None:
            if config.session_transcript_root is None:
                raise RuntimeError(
                    "adoption_repositories_enabled requires a transcript root")
            from hub.infrastructure.transcript_repository import (
                TranscriptRepository,
            )
            from tools.session.crypto import load_encryption_key

            sessions.transcript_repo = TranscriptRepository(
                config.session_transcript_root,
                key=load_encryption_key(config),
            )
            sessions.transcript_repo.init()
        # Task 12: the FINAL receipt hook must also notify the adoption
        # service.  When the Task 10 gate already wrapped the supervisor in a
        # ``SupervisorReceiptHook`` it is reused (its router stays intact);
        # otherwise the same thin hook is built now so EVERY receipt the hub
        # records converges on the ONE wrapper.  Adoption-disabled mode never
        # reaches this branch — the hook stays adoption-free and the receipt
        # flow is byte-identical to today.
        from hub.application.control_router import SupervisorReceiptHook

        if not isinstance(supervisor_service, SupervisorReceiptHook):
            supervisor_service = SupervisorReceiptHook(
                supervisor_service, None, session_repo=session_repo)
        adoption_service = AdoptionService(
            core.observation_repository, adoption_repo, supervisor_service,
            sessions.transcript_repo)
        supervisor_service.set_adoption(adoption_service)

    return ControlPlane(
        supervisor_service=supervisor_service,
        control_router=control_router,
        adoption_service=adoption_service,
    )
