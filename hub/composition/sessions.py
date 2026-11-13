"""Optional session (Task 6) repositories and services."""

from dataclasses import dataclass
from typing import Any


@dataclass
class SessionComponents:
    session_service: Any = None
    session_repo: Any = None
    transcript_repo: Any = None
    session_search_service: Any = None
    search_status: Any = None


def build_sessions(config, repositories: dict[str, Any]) -> SessionComponents:
    """Session services/repositories are optional.

    When enabled they use their own independent durable stores (session
    metadata + transcript), never the legacy observation/event/task paths.
    Imported lazily so the disabled default adds no import cost or startup
    behavior change.
    """
    s = SessionComponents()
    if not config.session_repositories_enabled:
        return s
    from hub.application.session_service import SessionService
    from hub.infrastructure.session_repository import SessionRepository
    from hub.infrastructure.transcript_repository import TranscriptRepository
    from fleet_core.session.crypto import load_encryption_key

    s.session_repo = repositories.get("sessions")
    if s.session_repo is None:
        if config.session_db is None:
            raise RuntimeError(
                "session_repositories_enabled requires a session_db path")
        s.session_repo = SessionRepository(config.session_db)
        s.session_repo.init()
    s.transcript_repo = repositories.get("transcript")
    if s.transcript_repo is None:
        if config.session_transcript_root is None:
            raise RuntimeError(
                "session_repositories_enabled requires a transcript root")
        s.transcript_repo = TranscriptRepository(
            config.session_transcript_root,
            key=load_encryption_key(config),
        )
        s.transcript_repo.init()
    if config.session_search_enabled:
        from hub.application.session_search_service import SessionSearchService
        from hub.infrastructure.session_search_repository import SessionSearchRepository
        search_repo = repositories.get("session_search")
        if search_repo is None:
            search_repo = SessionSearchRepository(
                config.session_search_db or config.root / "var" / "sessions" / "search.db",
                enabled=True,
            )
        s.session_search_service = SessionSearchService(search_repo)
        s.search_status = s.session_search_service.initialize_optional()
    s.session_service = SessionService(
        s.session_repo, s.transcript_repo, search_service=s.session_search_service)
    return s
