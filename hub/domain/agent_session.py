"""Fixed actions and tool identities for main-assistant fleet management."""

SESSION_CONTROL_ACTIONS = (
    'pause_session', 'resume_session', 'terminate_session',
    'quarantine_session', 'cancel_attempt',
)
SESSION_TOOLS = frozenset({
    'fleet.list_machines', 'session.list', 'session.get', 'session.read_events',
    'session.send_message', 'session.control', 'session.get_command',
})
