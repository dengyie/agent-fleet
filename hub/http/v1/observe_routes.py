"""hub/http/v1/observe_routes.py — 观测链路 /api/v1 兼容适配器

版本化的只读观测端点（status / machine detail / events / stream）直接复用
旧 ``hub.http.observe_routes`` 的公开 view 函数，绑定到显式 ``/api/v1`` 前缀下。

Design guarantee: these routes are the *same callables* the legacy ``/api/*``
routes serve, so service objects (``ObserveService``), SSE event/payload
mapping, sanitization, and ``error_response`` serialization have exactly one
implementation. No duplicated validation, state transitions, repository access,
or remote execution lives in this module.
"""
from flask import Blueprint

from hub.http.observe_routes import (
    api_events,
    api_machine,
    api_status,
    api_stream,
)

bp = Blueprint("observe_v1", __name__, url_prefix="/api/v1")

bp.route("/status", methods=("GET",), endpoint="v1_status")(api_status)
bp.route("/machines/<name>", methods=("GET",), endpoint="v1_machine_detail")(api_machine)
bp.route("/events", methods=("GET",), endpoint="v1_events")(api_events)
bp.route("/stream", methods=("GET",), endpoint="v1_stream")(api_stream)