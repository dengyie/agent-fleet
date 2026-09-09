"""hub/routes_observe.py — 观测蓝图兼容 re-export shim

Task 8 将路由迁移到薄 HTTP 适配器 ``hub.http.observe_routes``。本模块仅做
re-export，保持旧名称（``bp``、``build_summary``、``_sse_event_name``、
``_sse_payload``）对既有调用方（页面 SSR、SSE 测试、bootstrap）兼容；
**不包含任何业务逻辑**。
"""
from hub.http.observe_routes import (  # noqa: F401
    _sse_event_name,
    _sse_payload,
    bp,
    build_summary,
)

__all__ = ["bp", "build_summary", "_sse_event_name", "_sse_payload"]