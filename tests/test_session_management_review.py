"""Repros for replay and malformed structured-source management boundaries."""
import pytest

from tests.test_session_interactions import service, event, question
from hub.application.session_service import SessionServiceError
from tools.session.capture.hooks import HookAdapter


def test_capability_replay_cannot_reenable_a_later_revocation(service):
    service.ingest_events([event(3, "session_metadata", {"interactive_questions": False})])
    assert "interactive_questions" not in service.get_session("s1")["session"].get("capabilities", [])
    service.ingest_events([event(1, "session_start", {"agent_family": "claude", "adapter": "hooks", "interactive_questions": True})])
    assert "interactive_questions" not in service.get_session("s1")["session"].get("capabilities", [])


def test_response_before_request_does_not_reopen_an_answered_question(service):
    service.ingest_events([event(5, "interaction_response", {"request_id": "late-question", "selected": ["a"], "text": ""})])
    service.ingest_events([event(4, "interaction_request", {**question(), "request_id": "late-question"})])
    with pytest.raises(SessionServiceError, match="interaction_answered"):
        service.answer_interaction("s1", {"request_id": "late-question", "client_token": "answer-late", "selected": ["a"]})
    assert service.supervisor.commands == []


@pytest.mark.parametrize("kind", [[], {}, ["list"]])
def test_malformed_hook_question_type_is_rejected_without_escaping(kind):
    adapter = HookAdapter({"hooks": True, "interactive_questions": True})
    raw = {"type": "PreToolUse", "tool_name": "AskUserQuestion", "tool_use_id": "question-1",
           "tool_input": {"type": kind, "prompt": "Choose", "options": ["a"]}}
    assert adapter.interaction_event(raw) is None
