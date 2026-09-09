"""hub/http/v1/task_routes.py — task API /api/v1 兼容适配器

版本化任务端点（create / list / detail / files / cancel / retry）直接复用旧
``hub.http.task_routes`` 中已经过认证装饰器封装的 view 函数，绑定到显式
``/api/v1`` 前缀下。每个端点仍然是同一 callable：operator 认证域、
``require_task_store`` 降级、统一错误外型（含 request_id）与 public DTO 完全一致。
本模块**不**包含任何新的校验、状态转换或存储访问逻辑。
"""
from flask import Blueprint

from hub.http.task_routes import (
    cancel_task,
    create_task,
    get_task,
    get_task_file,
    list_task_files,
    list_tasks,
    retry_task,
)

bp = Blueprint("tasks_v1", __name__, url_prefix="/api/v1")

bp.route("/tasks", methods=("POST",), endpoint="v1_create")(create_task)
bp.route("/tasks", methods=("GET",), endpoint="v1_list")(list_tasks)
bp.route("/tasks/<task_id>", methods=("GET",), endpoint="v1_get")(get_task)
bp.route("/tasks/<task_id>/cancel", methods=("POST",), endpoint="v1_cancel")(cancel_task)
bp.route("/tasks/<task_id>/retry", methods=("POST",), endpoint="v1_retry")(retry_task)
bp.route("/tasks/<task_id>/files", methods=("GET",), endpoint="v1_list_files")(list_task_files)
bp.route("/tasks/<task_id>/files/<path:relpath>", methods=("GET",),
         endpoint="v1_get_file")(get_task_file)