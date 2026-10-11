"""Capability-gated answers use Supervisor with durable at-most-once receipts."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from hub.application.session_service import SessionService, SessionServiceError
from hub.infrastructure.session_repository import SessionRepository
from hub.infrastructure.transcript_repository import TranscriptRepository
from tools.session.capture.hooks import HookAdapter


def question():
    return {"request_id": "q1", "question_type": "multi", "prompt": "Choose",
            "options": [{"id": "a", "label": "First"}, {"id": "b", "label": "Second"}],
            "allow_custom": True, "tool_name": "AskUserQuestion"}


def event(sequence, kind, payload, managed=True):
    row = {"schema_version": 1, "event_id": "evt_" + str(sequence), "session_id": "s1",
        "machine_id": "m1", "stream_id": "st1", "sequence": sequence, "kind": kind,
        "payload": payload, "capture_quality": "structured" if managed else "best_effort",
        "source": "hooks", "emitted_at": "2026-10-11T00:00:00Z"}
    if managed:
        row["process_group_id"] = "g1"
    return row


class Supervisor:
    def __init__(self):
        self.commands = []

    def enqueue(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        return {"command_id": "cmd1"}


@pytest.fixture
def service(tmp_path):
    repo = SessionRepository(tmp_path / "sessions.db")
    repo.init()
    transcript = TranscriptRepository(tmp_path / "transcripts", key=bytes(range(32)))
    transcript.init()
    result = SessionService(repo, transcript)
    result.supervisor = Supervisor()
    result.ingest_events([event(1, "session_start", {"agent_family": "claude",
        "adapter": "hooks", "interactive_questions": True}), event(2, "interaction_request", question())])
    return result


def answer(service, **kwargs):
    data = {"request_id": "q1", "client_token": "answer-1", "selected": ["b", "a"], "text": ""}
    data.update(kwargs)
    return service.answer_interaction("s1", data)


def test_answer_idempotent_and_multi_order_stable(service):
    first = answer(service)
    assert answer(service, selected=["a", "b"]) == first
    assert len(service.supervisor.commands) == 1
    assert service.supervisor.commands[0][1]["payload"]["text"].index("First") < service.supervisor.commands[0][1]["payload"]["text"].index("Second")
    assert first["status"] == "queued"
    assert "interactive_questions" in service.get_session("s1")["session"]["capabilities"]


def test_concurrent_answer_only_enqueues_once(service):
    def submit(_):
        try:
            return answer(service)
        except SessionServiceError as exc:
            assert exc.code == "interaction_pending"
            return None
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(submit, range(8)))
    receipt = answer(service)
    assert all(row is None or row == receipt for row in rows)
    assert len(service.supervisor.commands) == 1


def test_interrupted_dispatch_cannot_enqueue_again(service):
    from hub.domain.interaction import normalize_answer
    data = {"selected": ["a"], "text": ""}
    normalized, _ = normalize_answer(question(), data)
    service.session_repo.reserve_interaction("s1", "q1", "answer-1", normalized)
    with pytest.raises(SessionServiceError, match="interaction_pending"):
        answer(service, selected=["a"])
    assert service.supervisor.commands == []


def test_restarted_service_keeps_receipt(service):
    first = answer(service)
    service.session_repo.init()
    fresh = SessionService(service.session_repo, service.transcript_repo)
    fresh.supervisor = Supervisor()
    assert answer(fresh) == first
    assert fresh.supervisor.commands == []


@pytest.mark.parametrize("change,code", [({"request_id": "missing"}, "interaction_expired"),
    ({"text": "a" * 2001}, "invalid_interaction_answer"),
    ({"selected": ["outside"]}, "invalid_interaction_answer"),
    ({"selected": ["a", "a"]}, "invalid_interaction_answer"),
    ({"client_token": ""}, "invalid_interaction_answer")])
def test_invalid_answer_has_fixed_code(service, change, code):
    with pytest.raises(SessionServiceError) as caught:
        answer(service, **change)
    assert caught.value.code == code
    assert service.supervisor.commands == []


def test_duplicate_other_token_rejected(service):
    answer(service)
    with pytest.raises(SessionServiceError, match="interaction_answered"):
        answer(service, client_token="answer-2")


def test_closed_question_rejected(service):
    service.ingest_events([event(3, "session_close", {"status": "closed", "reason": "done"})])
    with pytest.raises(SessionServiceError, match="interaction_expired"):
        answer(service)


def test_capability_required(service):
    service.session_repo.set_capabilities("s1", [])
    with pytest.raises(SessionServiceError, match="interaction_unavailable"):
        answer(service)


def test_replayed_id_cannot_replace_persisted_state(service):
    state = event(3, "agent_state_changed", {"state": "running", "source": "hook",
        "confidence": "high", "reason": "tool_started"})
    service.ingest_events([state])
    state["payload"]["state"] = "completed"
    service.ingest_events([state])
    assert service.get_session("s1")["session"]["current_state"] == "running"


def test_public_large_sequence_is_decimal_string(service):
    seq = (1 << 53) + 7
    service.ingest_events([event(seq, "assistant_message", {"text": "Large sequence", "is_complete": True})])
    result = service.session_events("s1", after_sequence=seq - 1)
    assert result["events"][0]["sequence"] == str(seq)


@pytest.mark.parametrize("kind", ["AskUserQuestion", "Inquirer"])
def test_hook_question_requires_capability(kind):
    raw = dict(question(), type=kind)
    assert HookAdapter({"hooks": True}).interaction_event(raw) is None
    result = HookAdapter({"hooks": True, "interactive_questions": True}).interaction_event(raw)
    assert result["kind"] == "interaction_request"
    assert result["payload"]["request_id"] == "q1"
