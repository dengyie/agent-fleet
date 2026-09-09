"""hub/routes_tasks.py — task 蓝图兼容 re-export shim

Task 8 将 task API 迁移到薄 HTTP 适配器 ``hub.http.task_routes``。本模块仅
re-export ``bp``，保持旧调用方（bootstrap / 现有测试）兼容；不含业务逻辑。
"""
from hub.http.task_routes import bp  # noqa: F401

__all__ = ["bp"]