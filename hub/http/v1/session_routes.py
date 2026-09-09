"""hub/http/v1/session_routes.py — session API /api/v1 兼容适配器（Task 6）

版本化端点直接复用旧 ``hub.http.session_routes`` 的公开 view 函数，绑定到
显式 ``/api/v1`` 前缀下。Design guarantee：这些路由是旧 ``/api/*`` 服务相同
的 callable，因此 SessionService、认证域装饰器、统一错误外型（含 request_id）
与 public DTO 只有一份实现。本模块不包含任何新的校验、状态转换或存储访问逻辑。

``/api/v1/session-events`` 是同一 ingest 服务的等价表面；``/api/v1/sessions``
查询端点与旧 ``/api/sessions`` 等价。本模块**不**暴露任何 supervisor 控制表面。
"""
from flask import Blueprint

from hub.http.session_routes import (
    api_session_events,
    api_session_events_query,
    api_session_detail,
    api_session_policy_signals,
    api_sessions,
)

bp = Blueprint("sessions_v1", __name__, url_prefix="/api/v1")

bp.route("/session-events", methods=("POST",),
         endpoint="v1_session_events")(api_session_events)
bp.route("/sessions", methods=("GET",),
         endpoint="v1_sessions")(api_sessions)
bp.route("/sessions/<session_id>", methods=("GET",),
         endpoint="v1_session_detail")(api_session_detail)
bp.route("/sessions/<session_id>/events", methods=("GET",),
         endpoint="v1_session_events_query")(api_session_events_query)
bp.route("/sessions/<session_id>/policy-signals", methods=("GET",),
         endpoint="v1_session_policy_signals")(api_session_policy_signals)