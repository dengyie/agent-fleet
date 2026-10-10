from pathlib import Path
import hashlib
import pytest

from hub.application.supervisor_service import SupervisorService
from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository
from hub.infrastructure.transcript_repository import TranscriptRepository
from tools.session.capture.hooks import HookAdapter
from hub.infrastructure.session_search_repository import SessionSearchRepository, SessionSearchRepositoryError
from unittest.mock import patch
import sqlite3
import threading


def test_supervisor_command_survives_restart_and_replays_idempotency(tmp_path):
    key = b"k" * 32
    db = tmp_path / "commands.db"
    first = SupervisorService(
        signing_key=key,
        command_repository=SupervisorCommandRepository(db),
    )
    command = first.enqueue("machine", "session", "attempt", "pause_session", "test",
                            idempotency_key="interaction:session:q:token")
    second = SupervisorService(
        signing_key=key,
        command_repository=SupervisorCommandRepository(db),
    )
    assert second.poll("machine")[0]["command_id"] == command["command_id"]
    replay = second.enqueue("machine", "session", "attempt", "pause_session", "test",
                            idempotency_key="interaction:session:q:token")
    assert replay["command_id"] == command["command_id"]


def test_hook_missing_event_id_does_not_dedupe_repeated_identical_prompts(tmp_path):
    adapter = HookAdapter({"hooks": True}, session_id="session", machine_id="machine",
                          stream_id="stream")
    raw = {"type": "UserPromptSubmit", "prompt": "same request"}
    first = adapter.normalize(raw)
    second = adapter.normalize(raw)
    assert first["event_id"] != second["event_id"]

    transcript = TranscriptRepository(Path(tmp_path) / "transcript.db")
    transcript.init()
    first["sequence"] = 1
    second["sequence"] = 2
    assert transcript.ingest(first).status == "accepted"
    assert transcript.ingest(second).status == "accepted"


def test_stream_sequence_conflict_is_not_ackable(tmp_path):
    repo = TranscriptRepository(Path(tmp_path) / "transcript.db")
    repo.init()
    base = {
        "schema_version": 1, "session_id": "session", "machine_id": "machine",
        "stream_id": "stream", "sequence": 1, "capture_quality": "best_effort",
        "source": "hook", "emitted_at": "2026-10-11T00:00:00Z",
        "kind": "assistant_message",
    }
    first = dict(base, event_id="event-a", payload={"text": "same", "is_complete": True})
    same = dict(base, event_id="event-b", payload={"text": "same", "is_complete": True})
    different = dict(base, event_id="event-c", payload={"text": "different", "is_complete": True})
    assert repo.ingest(first).status == "accepted"
    assert repo.ingest(same).status == "duplicate"
    assert repo.ingest(different).reason == "sequence_conflict"
    other_session = dict(first, event_id="event-other-session", session_id="other-session")
    assert repo.ingest(other_session).status == "accepted"
    other_stream = dict(first, event_id="event-other-stream", stream_id="stream-2")
    assert repo.ingest(other_stream).status == "accepted"


def test_search_failure_recovers_on_next_request(tmp_path):
    repo = SessionSearchRepository(Path(tmp_path) / "search.db", enabled=True)
    with patch.object(repo, "_connect", side_effect=sqlite3.OperationalError("temporary")):
        try:
            repo.init()
        except SessionSearchRepositoryError as exc:
            assert exc.code == "search_unavailable"
    repo.init()
    assert repo.available


def test_index_failure_is_replayed_from_bounded_redacted_queue(tmp_path):
    repo = SessionSearchRepository(Path(tmp_path) / "search.db", enabled=True)
    repo.init()
    event = {
        "event_id": "event", "session_id": "session", "sequence": 1,
        "kind": "assistant_message", "emitted_at": "2026-10-11T00:00:00Z",
        "payload": {"text": "replayed safely"},
    }
    connect = repo._connect
    failed = {"once": False}
    def transient_once():
        if not failed["once"]:
            failed["once"] = True
            raise sqlite3.OperationalError("transient")
        return connect()
    with patch.object(repo, "_connect", side_effect=transient_once):
        try:
            repo.index_event(event)
        except SessionSearchRepositoryError:
            pass
    repo.index_event(event)
    assert repo.search("replayed")["items"]


def test_search_pending_queue_is_bounded_and_pair_key_is_collision_free(tmp_path):
    repo = SessionSearchRepository(Path(tmp_path) / "search.db", enabled=True)
    repo.init()
    event = lambda session_id, event_id, sequence: {
        "event_id": event_id, "session_id": session_id, "sequence": sequence,
        "kind": "assistant_message", "emitted_at": "2026-10-11T00:00:00Z",
        "payload": {"text": f"pending-{sequence}"},
    }
    with patch("hub.infrastructure.session_search_repository.MAX_PENDING_EVENTS", 2):
        repo._queue_pending(event("a:b", "c", 1), machine_id=None,
                            project_id=None, agent_family=None)
        repo._queue_pending(event("a", "b:c", 1), machine_id=None,
                            project_id=None, agent_family=None)
        repo._queue_pending(event("third", "event", 1), machine_id=None,
                            project_id=None, agent_family=None)
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_pending").fetchone()[0] == 2
        keys = [row[0] for row in conn.execute(
            "SELECT event_key FROM search_pending ORDER BY id")]
    assert len(set(keys)) == 2


def test_search_pending_replay_drains_beyond_one_batch(tmp_path):
    repo = SessionSearchRepository(Path(tmp_path) / "search.db", enabled=True)
    repo.init()
    for sequence in range(1, 126):
        repo._queue_pending({
            "event_id": f"event-{sequence}", "session_id": "session",
            "sequence": sequence, "kind": "assistant_message",
            "emitted_at": "2026-10-11T00:00:00Z",
            "payload": {"text": f"pending-{sequence}"},
        }, machine_id=None, project_id=None, agent_family=None)

    repo.init()
    with sqlite3.connect(repo.db_path) as conn:
        pending = conn.execute("SELECT COUNT(*) FROM search_pending").fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM search_entries").fetchone()[0]
    assert pending == 0
    assert indexed == 125


def test_durable_receipt_first_winner_is_reported_under_concurrency(tmp_path):
    repo = SupervisorCommandRepository(Path(tmp_path) / "commands.db")
    service = SupervisorService(signing_key=b"k" * 32, command_repository=repo)
    command = service.enqueue("machine", "session", None, "pause_session", "test")
    results = []
    barrier = threading.Barrier(2)

    def receive(status):
        barrier.wait()
        results.append(service.receipt(command["command_id"], status, status))

    threads = [threading.Thread(target=receive, args=("succeeded",)),
               threading.Thread(target=receive, args=("failed",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(result["first"] for result in results) == [False, True]
    assert len({result["status"] for result in results}) == 1


def test_interaction_finish_failure_reconciles_existing_command(tmp_path):
    from hub.application.session_service import SessionService, SessionServiceError
    from hub.infrastructure.session_repository import SessionRepository
    from hub.infrastructure.transcript_repository import TranscriptRepository

    sessions = SessionRepository(Path(tmp_path) / "sessions.db")
    sessions.init()
    transcript = TranscriptRepository(Path(tmp_path) / "transcript.db")
    transcript.init()
    service = SessionService(sessions, transcript)
    service.supervisor = SupervisorService(
        signing_key=b"k" * 32,
        append_user_turn_enabled=True,
        command_repository=SupervisorCommandRepository(Path(tmp_path) / "commands.db"),
    )
    service.ingest_events([{
        "schema_version": 1, "event_id": "start", "session_id": "session",
        "machine_id": "machine", "stream_id": "stream", "sequence": 1,
        "kind": "session_start", "capture_quality": "structured", "source": "hook",
        "process_group_id": "group", "emitted_at": "2026-10-11T00:00:00Z",
        "payload": {"agent_family": "claude", "adapter": "hooks", "interactive_questions": True},
    }, {
        "schema_version": 1, "event_id": "question", "session_id": "session",
        "machine_id": "machine", "stream_id": "stream", "sequence": 2,
        "kind": "interaction_request", "capture_quality": "structured", "source": "hook",
        "process_group_id": "group", "emitted_at": "2026-10-11T00:00:01Z",
        "payload": {"request_id": "q", "question_type": "text", "prompt": "Answer",
                    "options": [], "allow_custom": True, "tool_name": "AskUserQuestion"},
    }])
    original_finish = sessions.finish_interaction
    state = {"failed": False}
    def fail_once(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise OSError("simulated sqlite failure")
        return original_finish(*args, **kwargs)
    sessions.finish_interaction = fail_once
    answer = {"request_id": "q", "client_token": "token", "text": "yes"}
    try:
        service.answer_interaction("session", answer)
    except SessionServiceError as exc:
        assert exc.code == "interaction_pending"
    assert service.answer_interaction("session", answer)["status"] == "queued"


def test_interaction_retry_recovers_crash_before_command_enqueue(tmp_path):
    from hub.application.session_service import SessionService, SessionServiceError
    from hub.domain.interaction import normalize_answer
    from hub.infrastructure.session_repository import SessionRepository
    from hub.infrastructure.transcript_repository import TranscriptRepository

    sessions = SessionRepository(Path(tmp_path) / "sessions.db")
    sessions.init()
    transcript = TranscriptRepository(Path(tmp_path) / "transcript.db")
    transcript.init()
    commands = SupervisorCommandRepository(Path(tmp_path) / "commands.db")
    supervisor = SupervisorService(
        signing_key=b"k" * 32, append_user_turn_enabled=True,
        command_repository=commands,
    )
    service = SessionService(sessions, transcript)
    service.supervisor = supervisor
    service.ingest_events([{
        "schema_version": 1, "event_id": "start", "session_id": "session",
        "machine_id": "machine", "stream_id": "stream", "sequence": 1,
        "kind": "session_start", "capture_quality": "structured", "source": "hook",
        "process_group_id": "group", "emitted_at": "2026-10-11T00:00:00Z",
        "payload": {"agent_family": "claude", "adapter": "hooks", "interactive_questions": True},
    }, {
        "schema_version": 1, "event_id": "question", "session_id": "session",
        "machine_id": "machine", "stream_id": "stream", "sequence": 2,
        "kind": "interaction_request", "capture_quality": "structured", "source": "hook",
        "process_group_id": "group", "emitted_at": "2026-10-11T00:00:01Z",
        "payload": {"request_id": "q", "question_type": "text", "prompt": "Answer",
                    "options": [], "allow_custom": True, "tool_name": "AskUserQuestion"},
    }])

    request = {"request_id": "q", "client_token": "token", "text": "yes"}
    normalized, _ = normalize_answer(sessions.get_interaction("session", "q"), request)
    sessions.reserve_interaction("session", "q", "token", normalized)
    recovered = service.answer_interaction("session", request)
    assert recovered["status"] == "queued"
    command = supervisor.find_idempotency(
        "interaction:" + hashlib.sha256(b"session\0q\0token").hexdigest())
    assert command is not None
    assert len(supervisor.poll("machine")) == 1


def test_concurrent_interaction_retries_both_return_same_command(tmp_path):
    from hub.application.session_service import SessionService
    from hub.infrastructure.session_repository import SessionRepository
    from hub.infrastructure.transcript_repository import TranscriptRepository

    sessions = SessionRepository(Path(tmp_path) / "sessions.db")
    sessions.init()
    transcript = TranscriptRepository(Path(tmp_path) / "transcript.db")
    transcript.init()
    commands = SupervisorCommandRepository(Path(tmp_path) / "commands.db")
    supervisor = SupervisorService(
        signing_key=b"k" * 32, append_user_turn_enabled=True,
        command_repository=commands,
    )
    service = SessionService(sessions, transcript)
    service.supervisor = supervisor
    service.ingest_events([{
        "schema_version": 1, "event_id": "start", "session_id": "session",
        "machine_id": "machine", "stream_id": "stream", "sequence": 1,
        "kind": "session_start", "capture_quality": "structured", "source": "hook",
        "process_group_id": "group", "emitted_at": "2026-10-11T00:00:00Z",
        "payload": {"agent_family": "claude", "adapter": "hooks", "interactive_questions": True},
    }, {
        "schema_version": 1, "event_id": "question", "session_id": "session",
        "machine_id": "machine", "stream_id": "stream", "sequence": 2,
        "kind": "interaction_request", "capture_quality": "structured", "source": "hook",
        "process_group_id": "group", "emitted_at": "2026-10-11T00:00:01Z",
        "payload": {"request_id": "q", "question_type": "text", "prompt": "Answer",
                    "options": [], "allow_custom": True, "tool_name": "AskUserQuestion"},
    }])

    enqueue_barrier = threading.Barrier(2)
    original_enqueue = supervisor.enqueue

    def synchronized_enqueue(machine, session_id, attempt_id, action, reason_code,
                             *, nonce=None, candidate=None, payload=None,
                             idempotency_key=None):
        enqueue_barrier.wait(timeout=5)
        return original_enqueue(
            machine, session_id, attempt_id, action, reason_code,
            nonce=nonce, candidate=candidate, payload=payload,
            idempotency_key=idempotency_key)

    supervisor.enqueue = synchronized_enqueue
    results = []
    errors = []
    request = {"request_id": "q", "client_token": "token", "text": "yes"}

    def answer():
        try:
            results.append(service.answer_interaction("session", request))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=answer), threading.Thread(target=answer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0]["command_id"] == results[1]["command_id"]
    assert all(result["status"] == "queued" for result in results)
    assert len(supervisor.poll("machine")) == 1


def test_supervisor_receipt_progress_never_regresses(tmp_path):
    repo = SupervisorCommandRepository(Path(tmp_path) / "commands.db")
    service = SupervisorService(signing_key=b"k" * 32, command_repository=repo)
    command = service.enqueue("machine", "session", None, "pause_session", "test")

    accepted = service.receipt(command["command_id"], "accepted", "queued")
    duplicate = service.receipt(command["command_id"], "accepted", "duplicate")
    executing = service.receipt(command["command_id"], "executing", "started")
    stale = service.receipt(command["command_id"], "accepted", "queued")
    assert accepted["first"] is True
    assert duplicate["first"] is False
    assert duplicate["reason"] == "queued"
    assert executing["first"] is False
    assert executing["status"] == "executing"
    assert stale["status"] == "executing"
    assert stale["reason"] == "started"
    assert stale["first"] is False


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_supervisor_receipt_storage_failures_are_typed(tmp_path, fail_on_call):
    from hub.application.supervisor_service import SupervisorServiceError
    repo = SupervisorCommandRepository(Path(tmp_path) / "commands.db")
    service = SupervisorService(signing_key=b"k" * 32, command_repository=repo)
    command = service.enqueue("machine", "session", None, "pause_session", "test")
    connect = sqlite3.connect
    calls = 0

    def fail_selected_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise sqlite3.OperationalError("database unavailable")
        return connect(*args, **kwargs)

    with patch("hub.infrastructure.supervisor_command_repository.sqlite3.connect",
               side_effect=fail_selected_call):
        with pytest.raises(SupervisorServiceError) as caught:
            service.receipt(command["command_id"], "succeeded", "done")
    assert caught.value.code == "command_unavailable"
    assert caught.value.status == 503
