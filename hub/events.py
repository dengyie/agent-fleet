"""hub/events.py — 事件总线（兼容门面）

The event system has been refactored into:

- ``hub.domain.events`` — event shape definitions
- ``hub.infrastructure.event_repository.JsonlEventRepository`` — persistence
- ``hub.application.event_publisher.EventPublisher`` — creation + distribution

This module remains the public API for all existing callers. It lazily
constructs a default ``EventPublisher`` backed by a ``JsonlEventRepository``
pointed at ``EVENT_LOG``. When ``EVENT_LOG`` is reassigned (for tests /
bootstrap path override) the publisher is rebuilt on the next call.

New code should inject an ``EventPublisher`` directly and avoid the
module-level path.
"""

from pathlib import Path

from hub.application.event_publisher import EventPublisher
from hub.infrastructure.event_repository import JsonlEventRepository

FLEET_HOME = Path(__file__).resolve().parent.parent
EVENT_LOG = FLEET_HOME / "state" / "events.jsonl"

# Lazily-built fallback publisher. Rebuilt when EVENT_LOG changes.
_saved_log = None
_publisher = None
_legacy_sse_unsubscribers = {}


def _default_publisher():
    global _saved_log, _publisher
    if _publisher is None or _saved_log != EVENT_LOG:
        _publisher = EventPublisher(
            JsonlEventRepository(EVENT_LOG, max_bytes=1_000_000, keep_lines=1000),
        )
        _saved_log = EVENT_LOG
    return _publisher


def _active_publisher():
    """Return the request's publisher, falling back to the CLI publisher."""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            fleet = current_app.extensions.get("fleet", {})
            publisher = fleet.get("publisher")
            if publisher is not None:
                return publisher
    except (ImportError, RuntimeError, AttributeError):
        pass
    return _default_publisher()


def set_publisher(publisher):
    """Override the default publisher with an injected one (bootstrap).

    Call this after ``_wire_legacy_paths`` so the publisher is ready for the
    first subscriber registration.
    """
    global _publisher, _saved_log
    _publisher = publisher
    _saved_log = EVENT_LOG


def emit(event_type, machine=None, changes=None, snapshot=None, **extra):
    """发出一个事件。event_type 如 'state_changed' / 'alert' / 'recovered'。"""
    return _active_publisher().emit(event_type, machine=machine, changes=changes,
                                      snapshot=snapshot, **extra)


def subscribe(cb):
    """注册事件订阅者。cb(ev) 接收事件 dict。"""
    _active_publisher().subscribe(cb)


def read_recent(limit=50):
    """读取最近事件（供 web/history 展示）"""
    return _active_publisher().read_recent(limit)


def sse_subscribe(maxsize=200):
    """注册一个 SSE 队列订阅者。返回 queue.Queue。"""
    publisher = _active_publisher()
    q, unsubscribe = publisher.subscribe_sse(queue_size=maxsize)
    _legacy_sse_unsubscribers[id(q)] = (publisher, unsubscribe)
    return q


def sse_unsubscribe(q):
    """注销一个 SSE 队列订阅者。

    The legacy API only has the queue reference, not the unsubscribe handle.
    The publisher maintains a reverse-lookup table so we can unsubscribe by
    queue identity.
    """
    pair = _legacy_sse_unsubscribers.pop(id(q), None)
    if pair is not None:
        pair[1]()
