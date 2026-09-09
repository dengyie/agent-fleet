"""hub/auth.py — 认证域装饰器、凭据解析与 transport-only 身份提取

三个认证域（蓝图边界 = 认证边界）：
  observe  — ingest token（机器上报）
  tasks    — CF Access operator（Phase 2 实现 require_operator）
  commands — runner credential（Phase 2 实现 require_runner）

Task 8 新增 transport-only 身份提取原语，供薄 HTTP 适配器复用：

* ``extract_operator_identity(request) -> str | None`` — 仅从
  ``Cf-Access-Authenticated-User-Email`` 读取；缺失时在*未携带其他域凭据头*
  的场合回退 ``app.config["DEV_OPERATOR"]``，否则返回 ``None``。**foreign
  domain header 永远阻止 DEV_OPERATOR fallback。**
* ``extract_runner_identity(request) -> tuple[str, str] | None`` — 校验
  ``X-Runner-Credential: <machine>:<secret>``；安全机器名 + 凭据比对一致时记录
  ``g.runner_machine`` 并返回 ``(machine, secret)``。
* ``extract_ingest_token(request) -> str`` — 返回 ingest 头值（缺省空串）。

认证域互斥逻辑只存在于这些提取函数；装饰器复用之，保持既有守卫行为
（403 forbidden / 401 unauthorized、DEV fallback 条件）不变。
"""
import functools
import hmac
import json
import os
import re
from pathlib import Path

from flask import current_app, g, request

from hub.http.errors import ApplicationError, error_response

FLEET_HOME = Path(__file__).resolve().parent.parent
INGEST_TOKEN_FILE = FLEET_HOME / "credentials" / "ingest-token"
INGEST_TOKEN_ENV = "AGENT_FLEET_INGEST_TOKEN"
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUNNER_CREDENTIALS_FILE = FLEET_HOME / "credentials" / "runner-credentials.json"

_AF_TOKEN_HEADER = "X-Agent-Fleet-Token"
_RUNNER_CREDENTIAL_HEADER = "X-Runner-Credential"
_CF_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"
_SUPERVISOR_CREDENTIAL_HEADER = "X-Supervisor-Credential"

#: 其他认证域的凭据头。携带这些头的请求，即使配置了 DEV_OPERATOR 也不回退成 operator 身份，
#: 否则 runner 凭据或 ingest token 可冒充 operator 调用任务 API。蓝图边界 = 认证边界。
_FOREIGN_IDENTITY_HEADERS = (
    _AF_TOKEN_HEADER,
    _RUNNER_CREDENTIAL_HEADER,
    _SUPERVISOR_CREDENTIAL_HEADER,
)


def load_supervisor_credentials():
    """Per-machine supervisor credential mapping (never logged/printed).

    Source priority: env JSON (``AGENT_FLEET_SUPERVISOR_CREDENTIALS``) then a
    ``credentials/supervisor-credentials.json`` file (0600, not in git).
    No credential value ever appears in a string/repr/exception.
    """
    from hub.domain import supervisor as sup_dom

    env = os.environ.get(sup_dom.SUPERVISOR_CREDENTIAL_ENV)
    if env:
        try:
            data = json.loads(env)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if k and v}
        except (ValueError, TypeError):
            pass
    try:
        path = FLEET_HOME / "credentials" / "supervisor-credentials.json"
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if k and v}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def load_supervisor_signing_key():
    """Load the Hub Ed25519 signing private key (base64 of 32 raw bytes).

    Priority: env ``AGENT_FLEET_SUPERVISOR_SIGNING_KEY`` then a
    ``credentials/supervisor-signing.key`` file.  Returns ``None`` when absent
    or malformed — callers must fail closed (no issuance, 503 supervisor
    commands).  The key VALUE is never printed or included in any error string.
    """
    import base64 as _b64

    def _decode(value):
        if not isinstance(value, str) or not value:
            return None
        try:
            raw = _b64.b64decode(value.strip(), validate=True)
        except (ValueError, TypeError):
            return None
        return raw if len(raw) == 32 else None

    env = os.environ.get("AGENT_FLEET_SUPERVISOR_SIGNING_KEY")
    if env:
        decoded = _decode(env)
        if decoded is not None:
            return decoded
    try:
        path = FLEET_HOME / "credentials" / "supervisor-signing.key"
        if path.exists():
            return _decode(path.read_text().strip())
    except OSError:
        return None
    return None


def extract_supervisor_identity(req=None) -> tuple[str, str] | None:
    """Extract a valid ``(machine, secret)`` from the supervisor credential
    header, or ``None``.

    Never falls back to any other identity domain (foreign headers never pass,
    and this credential can never fall back to DEV_OPERATOR).
    """
    src = request if req is None else req
    raw = src.headers.get(_SUPERVISOR_CREDENTIAL_HEADER)
    if not raw:
        return None
    machine, _, secret = raw.partition(":")
    if not machine or not secret or ":" in secret:
        return None
    if not MACHINE_RE.fullmatch(machine):
        return None
    creds = current_app.config.get("SUPERVISOR_CREDENTIALS")
    if creds is None:
        creds = load_supervisor_credentials()
    expected = creds.get(machine) if isinstance(creds, dict) else None
    if not expected or not hmac.compare_digest(secret, expected):
        return None
    g.supervisor_machine = machine
    return (machine, secret)


def require_supervisor(view):
    """supervisor 域：X-Supervisor-Credential: <machine>:<secret> 机器绑定认证。

    只认证凭据本身；签名 key 门禁由服务层负责。失败 → 403。
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        identity = extract_supervisor_identity()
        if identity is None:
            return _forbidden()
        return view(*args, **kwargs)
    return wrapper


def resolve_ingest_token(cli_token=None):
    """ingest 令牌解析优先级：CLI > env > credentials/ingest-token 文件。"""
    if cli_token:
        return cli_token
    env = os.environ.get(INGEST_TOKEN_ENV)
    if env:
        return env
    try:
        if INGEST_TOKEN_FILE.exists():
            return INGEST_TOKEN_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def load_runner_credentials():
    """每机独立 runner 凭据：credentials/runner-credentials.json {"<machine>": "<secret>"}。

    文件须 0600、不进 git；单台机器吊销 = 删对应键。
    """
    try:
        if RUNNER_CREDENTIALS_FILE.exists():
            data = json.loads(RUNNER_CREDENTIALS_FILE.read_text())
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _carries_foreign_identity_header() -> bool:
    """请求携带了其他认证域的凭据头（ingest token 或 runner credential）。"""
    return any(request.headers.get(h) for h in _FOREIGN_IDENTITY_HEADERS)


# ---------------------------------------------------------------------------
# transport-only 身份提取
# ---------------------------------------------------------------------------

def extract_operator_identity(req=None) -> str | None:
    """从请求提取 operator 身份：CF Email 头 → DEV_OPERATOR 兜底 → None。

    携带 foreign-domain header 时永不回退 DEV_OPERATOR（auth-domain 互斥）。
    """
    if req is not None:
        email = req.headers.get(_CF_EMAIL_HEADER, "").strip()
        if email:
            return email
        if any(req.headers.get(h) for h in _FOREIGN_IDENTITY_HEADERS):
            return None
        return current_app.config.get("DEV_OPERATOR") or None
    email = request.headers.get(_CF_EMAIL_HEADER, "").strip()
    if email:
        return email
    if _carries_foreign_identity_header():
        return None
    return current_app.config.get("DEV_OPERATOR") or None


def extract_runner_identity(repo_request=None) -> tuple[str, str] | None:
    """从请求提取 runner 身份 ``(machine, secret)``；无效时返回 None。

    认证成功时记录 ``g.runner_machine``（供适配器把机器传给应用服务）；
    适配器绝不用 runner JSON 里的 machine 字段。
    """
    header_source = request if repo_request is None else repo_request
    header = header_source.headers.get(_RUNNER_CREDENTIAL_HEADER, "")
    machine, _, secret = header.partition(":")
    if not machine or not secret or not MACHINE_RE.fullmatch(machine):
        return None
    creds = current_app.config.get("RUNNER_CREDENTIALS")
    if creds is None:
        creds = load_runner_credentials()
    expected = creds.get(machine) if isinstance(creds, dict) else None
    if not expected or not hmac.compare_digest(secret, expected):
        return None
    g.runner_machine = machine
    return (machine, secret)


def extract_ingest_token(repo_request=None) -> str:
    """返回 ingest token 头值（无则空串）。"""
    if repo_request is not None:
        return repo_request.headers.get(_AF_TOKEN_HEADER, "")
    return request.headers.get(_AF_TOKEN_HEADER, "")


# ---------------------------------------------------------------------------
# 认证域装饰器（保持历史行为与状态码）
# ---------------------------------------------------------------------------

def _forbidden():
    err = ApplicationError("forbidden", "", 403)
    return error_response(err)


def require_ingest_token(view):
    """observe 域：X-Agent-Fleet-Token 头与 app.config['INGEST_TOKEN'] 比对。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("INGEST_TOKEN")
        if token:
            got = extract_ingest_token()
            if not hmac.compare_digest(got, token):
                return _forbidden()
        return view(*args, **kwargs)
    return wrapper


def require_operator(view):
    """tasks 域：CF Access 认证（或仅限未携带他域凭据头的 DEV fallback）。
    没有身份 → 401（统一错误外型）。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        email = extract_operator_identity()
        if not email:
            return error_response(
                ApplicationError("unauthorized", "operator identity required", 401))
        g.operator = email
        return view(*args, **kwargs)
    return wrapper


def require_runner(view):
    """commands 域：X-Runner-Credential: <machine>:<secret>，凭据与机器绑定。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        identity = extract_runner_identity()
        if identity is None:
            return _forbidden()
        return view(*args, **kwargs)
    return wrapper


def require_task_store(view):
    """SQLite 不可用降级：任务 API 跳过，观测链路不受影响（统一错误外型）。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.config.get("TASKS_ENABLED"):
            return error_response(
                ApplicationError("tasks_unavailable", "任务存储不可用，观测链路不受影响", 503))
        return view(*args, **kwargs)
    return wrapper