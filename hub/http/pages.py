"""hub/http/pages.py — SSR 页面路由（TemplateView adapter）

保留原有页面行为与响应（状态码、渲染模板、错误文案）：
  GET /             fleet 总览
  GET /machine/<n>  单机页（+ 任务列表、新建任务按钮）
  GET /task/<tid>   任务页

默认（cutover 关闭）仍渲染兼容模板，只通过 legacy 门面（``build_summary``、
events facade、``state`` / ``task_store``）读取展示数据；**不执行任何业务策略**。
私有/内部键（attempt_id、client_token）不会进入模板上下文。

静态前端 cutover：当 ``FRONTEND_CUTOVER`` 开启时，三个页面路由改为返回
``frontend/index.html`` shell（仅注入 origin-root base href，保证嵌套路径
相对资源解析正确；保持浏览器 URL 不变，由前端路由解码真实页面）。shell
缺失时失败关闭为有界非敏感错误，绝不回退到业务策略或泄露后端细节。
"""
import time
from pathlib import Path

from flask import Blueprint, abort as flask_abort, current_app, render_template, send_file

from hub import auth as auth_module
from hub import events as event_store
from hub import state as state_store
from hub import task_store
from hub import web as web_module
from hub.http.observe_routes import build_summary
from report_schema import sanitize_agents, sanitize_system

bp = Blueprint("pages", __name__)

# 有界、非敏感的失败关闭文案（shell 缺失时不泄露路径/异常细节）
_SHELL_MISSING = "frontend shell unavailable"


def _cutover_enabled() -> bool:
    """静态前端 cutover 开关（默认关闭）。"""
    return bool(current_app.config.get("FRONTEND_CUTOVER", False))


def _read_shell() -> str | None:
    """读取 ``frontend_dir/index.html``。

    仅返回静态文件内容（非敏感）；任何缺失/读取失败都返回 None，由
    调用方失败关闭。不注入凭据、不访问后端状态。
    """
    frontend_dir = current_app.config.get("FRONTEND_DIR")
    if not frontend_dir:
        return None
    try:
        shell = Path(frontend_dir) / "index.html"
        if not shell.is_file():
            return None
        return shell.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _serve_shell():
    """以 cutover 模式返回静态前端 shell。

    保持浏览器 URL 不变（前端路由据此解码真实页面）。注入 origin-root
    base href，使 ``/machine/<n>``、``/task/<id>`` 等嵌套路径下的相对
    资源（``./config.js``、``./styles/app.css``…）解析到站点根，无需
    改动 Nginx/Cloudflare 配置。shell 缺失时失败关闭为有界非敏感响应。
    """
    html = _read_shell()
    if html is None:
        return _SHELL_MISSING, 404
    # 注入 origin-root base href：仅当 <head> 存在时替换，避免重复注入。
    if "<head>" in html:
        html = html.replace("<head>", '<head>\n<base href="/">', 1)
    return html, 200


# 仅允许与前端 release 布局一致的相对资源前缀（其余路径交给各自路由或
# 统一的 /api/* JSON 错误契约，绝不让资产路由吞掉任意 API 路径）。
_ALLOWED_ASSET_PREFIXES = (
    "config.js", "routes.js",
    "assets/", "styles/", "api/", "views/", "realtime/", "state/",
)


def _serve_asset(filepath: str):
    """cutover 开启时从 ``frontend_dir`` 提供静态前端资产（同应用 Flask 服务）。

    前端 shell 通过注入的 ``<base href="/">`` 把相对资源解析到站点根
    （``/config.js``、``/styles/app.css``、``/api/client.js``…），因此本应用
    需要显式为这些资源提供路由，使 cutover 无需外部代理改动即可独立使用。

    安全边界：
    - 仅 cutover 开启时响应（关闭时交给统一错误契约，直接 404）。
    - 路径前缀白名单，绝不让任意 ``/api/*`` 或其它路径落到响应解析。
    - 显式拒绝 ``.`` / ``..`` 路径段（仅前缀白名单不够：
      ``api/../secret.txt`` 能过 ``api/`` 前缀、且解析后仍留在
      ``frontend_dir`` 内，``relative_to`` 拦不住；必须在解析前逐段拒绝）。
    - ``resolve()`` 后再用 ``relative_to`` 校验结果仍位于 ``frontend_dir``
      内，阻断目录穿越（含编码穿越等）。
    - 仅下发实际文件（``is_file()``），目录／缺失一律 404。
    - 缺失/越界/读取失败一律 ``abort(404)``，由 ``register_error_handlers``
      输出有界非敏感响应；不泄露路径、异常详情或凭据。
    """
    if not _cutover_enabled():
        flask_abort(404)
    frontend_dir = current_app.config.get("FRONTEND_DIR")
    if not frontend_dir:
        flask_abort(404)
    if not filepath.startswith(_ALLOWED_ASSET_PREFIXES):
        flask_abort(404)
    # 逐段拒绝 ``.`` / ``..``（Flask URL 解码之后）：``api/../secret.txt``
    # 能过 ``api/`` 前缀且仍留在 frontend_dir 内，必须在此拦截。
    if any(seg in (".", "..") for seg in filepath.split("/")):
        flask_abort(404)
    if filepath.startswith("/") or filepath.startswith("\\"):
        flask_abort(404)
    try:
        frontend_root = Path(frontend_dir).resolve()
        target = (frontend_root / filepath).resolve()
        # 解析结果必须仍在 frontend_dir 内（相对 to 校验，防目录穿越）
        target.relative_to(frontend_root)
        if not target.is_file():
            flask_abort(404)
        return send_file(target)
    except (OSError, ValueError):
        flask_abort(404)


@bp.route("/")
def index():
    if _cutover_enabled():
        return _serve_shell()
    rows = build_summary()
    recent = [
        {
            "machine": event.get("machine"),
            "event": event.get("event"),
            "ts": time.strftime("%H:%M:%S", time.localtime(event.get("ts") or 0)),
        }
        for event in event_store.read_recent(20)
    ]
    return render_template("fleet.html", rows=rows, events=recent)


@bp.route("/machine/<name>")
def machine_view(name):
    # cutover 模式：任何 /machine/<n> 都回前端 shell，浏览器 URL 不变；
    # 机器名校验交给前端路由（保持 shell 可服务任意合法路径段）。
    if _cutover_enabled():
        return _serve_shell()
    if not auth_module.MACHINE_RE.fullmatch(name):
        return "invalid machine name", 400
    current = state_store.read_current(name)
    if not current:
        return "machine not found", 404
    tasks = []
    if current_app.config.get("TASKS_ENABLED"):
        try:
            tasks = task_store.list_tasks(machine=name, limit=10)
        except Exception:
            tasks = []
    desc = next(
        (host["desc"] for host in web_module.load_hosts() if host["name"] == name),
        "自报告 (ingest)",
    )
    return render_template(
        "machine.html",
        name=name,
        desc=desc,
        reachable=bool(current.get("reachable", True)),
        error=current.get("remote_error"),
        agents=sanitize_agents(current.get("agents", {})),
        system=sanitize_system(current.get("system", {})),
        tasks=tasks,
    )


@bp.route("/task/<task_id>")
def task_view(task_id):
    # cutover 模式：/task/<id> 直接回前端 shell（浏览器 URL 保留，任务 id
    # 由前端路由解码并从 API 获取）；任务是否存在交给前端展示。
    if _cutover_enabled():
        return _serve_shell()
    if not current_app.config.get("TASKS_ENABLED"):
        return "tasks unavailable", 503
    task = task_store.get_task(task_id)
    if not task:
        return "task not found", 404
    return render_template("task.html", task=task)


@bp.route("/<path:filepath>")
def frontend_asset(filepath):
    """静态前端资产路由：cutover 开启时从 ``frontend_dir`` 提供资源。

    该兜底路由只命中未被更具体路由（``/``、``/machine/<n>``、``/task/<id>``、
    ``/api/*``、``/static/*``）匹配的路径。cutover 关闭或路径不合法时通过
    ``abort(404)`` 交给统一错误契约（``/api/*`` 路径保持 JSON 有界响应）。
    """
    return _serve_asset(filepath)