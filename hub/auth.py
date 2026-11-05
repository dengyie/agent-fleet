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
import logging
import os
import re
from pathlib import Path

from flask import current_app, g, request

from hub.http.errors import ApplicationError, error_response

logger = logging.getLogger(__name__)

FLEET_HOME = Path(__file__).resolve().parent.parent
INGEST_TOKEN_FILE = FLEET_HOME / "credentials" / "ingest-token"
INGEST_TOKEN_ENV = "AGENT_FLEET_INGEST_TOKEN"
MACHINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUNNER_CREDENTIALS_FILE = FLEET_HOME / "credentials" / "runner-credentials.json"

_AF_TOKEN_HEADER = "X-Agent-Fleet-Token"
_RUNNER_CREDENTIAL_HEADER = "X-Runner-Credential"
_CF_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"
_SUPERVISOR_CREDENTIAL_HEADER = "X-Supervisor-Credential"
_PLATFORM_NODE_CREDENTIAL_HEADER = "X-Platform-Node-Credential"

#: 其他认证域的凭据头。携带这些头的请求，即使配置了 DEV_OPERATOR 也不回退成 operator 身份，
#: 否则 runner 凭据或 ingest token 可冒充 operator 调用任务 API。蓝图边界 = 认证边界。
#: 显式开发开关：未配置 ingest token 时才允许匿名写 ingest 端点。生产默认 fail closed。
ALLOW_INSECURE_INGEST_ENV = "AGENT_FLEET_ALLOW_INSECURE_INGEST"


class CredentialStoreUnavailable(RuntimeError):
    """凭据存储读取失败（与"凭据无效"区分）：守卫返回 503 而非 403。

    节点据此重试，而不会误以为自己的凭据被吊销。错误文本不含路径或凭据值。
    """

    def __init__(self, code: str = "credential_store_unavailable"):
        self.code = code
        super().__init__(code)


def _store_unavailable(code: str = "credential_store_unavailable"):
    return error_response(ApplicationError(code, "凭据存储暂不可用，请稍后重试", 503))


def mark_machine_auth(wrapper):
    """把 ``wrapper`` 显式登记为机器认证视图。

    标记值是 wrapper 自身：``functools.wraps`` 会把 ``__dict__`` 复制到外层
    装饰器，但复制过去的值指向内层函数，``is_machine_auth_view`` 只认
    ``view._fleet_machine_auth is view``，因此标记不会被外层 operator 装饰器
    意外继承（否则账户模式的 Origin/CSRF 边界会被跳过）。
    """
    wrapper._fleet_machine_auth = wrapper
    return wrapper


def is_machine_auth_view(view) -> bool:
    """注册到路由的视图函数本身是否为机器认证入口（不沿 ``__wrapped__`` 下钻）。"""
    return view is not None and getattr(view, "_fleet_machine_auth", None) is view


_FOREIGN_IDENTITY_HEADERS = (
    _AF_TOKEN_HEADER,
    _RUNNER_CREDENTIAL_HEADER,
    _SUPERVISOR_CREDENTIAL_HEADER,
    _PLATFORM_NODE_CREDENTIAL_HEADER,
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
    path = FLEET_HOME / "credentials" / "supervisor-credentials.json"
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        # 文件存在但读不出：存储故障，不是"无凭据"。
        raise CredentialStoreUnavailable() from None
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if k and v}
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


def load_platform_command_signing_key():
    """Load the distinct Hub key used for platform execution commands.

    The command key deliberately has its own environment/file namespace so a
    node cannot accidentally accept supervisor control-plane envelopes as
    platform workspace commands.  Values are decoded but never returned in a
    public response or included in an error string.
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

    env = os.environ.get("AGENT_FLEET_PLATFORM_COMMAND_SIGNING_KEY")
    if env:
        decoded = _decode(env)
        if decoded is not None:
            return decoded
    try:
        path = FLEET_HOME / "credentials" / "platform-command-signing.key"
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
        try:
            identity = extract_supervisor_identity()
        except CredentialStoreUnavailable as exc:
            return _store_unavailable(exc.code)
        if identity is None:
            return _forbidden()
        return view(*args, **kwargs)
    return mark_machine_auth(wrapper)


def extract_platform_node_identity(req=None) -> tuple[str, str] | None:
    """Authenticate a platform node against the platform repository.

    The header is ``<node_id>:<one-time-provisioned-secret>``. The secret is
    never returned in the identity tuple or stored in Flask ``g``.
    """
    src = request if req is None else req
    raw = src.headers.get(_PLATFORM_NODE_CREDENTIAL_HEADER, "")
    node_id, sep, secret = raw.partition(":")
    if not sep or not node_id or not secret or ":" in secret:
        return None
    if not MACHINE_RE.fullmatch(node_id):
        return None
    fleet = current_app.extensions.get("fleet", {})
    repository = fleet.get("platform_repository")
    if repository is None:
        return None
    from hub.infrastructure.platform_db import (
        PlatformRepositoryError,
        PlatformValidationError,
    )
    try:
        identity = repository.authenticate_node_credential(node_id, secret)
    except PlatformValidationError:
        return None
    except (PlatformRepositoryError, OSError) as exc:
        # 存储故障 ≠ 凭据无效：抛给守卫映射为 503，节点不会误判凭据失效。
        raise CredentialStoreUnavailable() from exc
    if not identity:
        return None
    g.platform_node_id = identity["node_id"]
    g.platform_node_owner = identity["owner_id"]
    return identity["owner_id"], identity["node_id"]


def require_platform_node(view):
    """Platform node domain; never falls back to operator identity."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            identity = extract_platform_node_identity()
        except CredentialStoreUnavailable as exc:
            return _store_unavailable(exc.code)
        if identity is None:
            return _forbidden()
        return view(*args, **kwargs)
    return mark_machine_auth(wrapper)


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
    except OSError:
        # 启动期解析：读不出 token 文件按"未配置"处理（ingest 端点随之 fail
        # closed 返回 503），但必须留下诊断，不能静默。
        logger.warning("ingest token file unreadable; ingest endpoints fail closed")
    return None


def load_runner_credentials():
    """每机独立 runner 凭据：credentials/runner-credentials.json {"<machine>": "<secret>"}。

    文件须 0600、不进 git；单台机器吊销 = 删对应键。
    """
    try:
        if not RUNNER_CREDENTIALS_FILE.exists():
            return {}
        data = json.loads(RUNNER_CREDENTIALS_FILE.read_text())
    except (OSError, ValueError, TypeError):
        # 文件存在但读不出/解析失败：存储故障（503），不是凭据无效（403）。
        raise CredentialStoreUnavailable() from None
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
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
    if current_app.config.get("ACCOUNTS_ENABLED"):
        from hub.accounts.http import account_identity
        user = account_identity(request if req is None else req)
        return user["id"] if user else None
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


def insecure_ingest_allowed() -> bool:
    """显式开发开关：``app.config['ALLOW_INSECURE_INGEST']`` 或 env=1。"""
    if current_app.config.get("ALLOW_INSECURE_INGEST"):
        return True
    return os.environ.get(ALLOW_INSECURE_INGEST_ENV, "").strip() == "1"


def require_ingest_token(view):
    """observe 域：X-Agent-Fleet-Token 头与 app.config['INGEST_TOKEN'] 比对。

    - 未配置 token → fail closed 503 ``ingest_token_not_configured``，除非显式
      开发开关 ``ALLOW_INSECURE_INGEST`` / ``AGENT_FLEET_ALLOW_INSECURE_INGEST=1``；
    - 请求额外携带 ``X-Runner-Credential`` 时校验该每机凭据：无效 → 403，有效
      → ``g.ingest_machine`` 绑定为凭据机器（适配器要求 payload machine 一致）；
      否则 ``g.ingest_machine = None``、``g.ingest_auth = "shared_token"``。
    """
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("INGEST_TOKEN")
        if token:
            got = extract_ingest_token()
            if not hmac.compare_digest(got, token):
                return _forbidden()
        elif not insecure_ingest_allowed():
            return error_response(ApplicationError(
                "ingest_token_not_configured", "ingest token 未配置", 503))
        g.ingest_machine = None
        g.ingest_auth = "shared_token"
        if request.headers.get(_RUNNER_CREDENTIAL_HEADER):
            try:
                identity = extract_runner_identity()
            except CredentialStoreUnavailable as exc:
                return _store_unavailable(exc.code)
            if identity is None:
                return _forbidden()
            g.ingest_machine = identity[0]
            g.ingest_auth = "machine_credential"
        return view(*args, **kwargs)
    return mark_machine_auth(wrapper)


_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


def operator_origin_allowed(req=None) -> bool:
    """非账户模式（CF Access / DEV）写请求的同源校验。

    CF Access 身份由边缘注入、DEV_OPERATOR 无需任何凭据，浏览器跨站提交会
    "自动带上"这两种身份，因此写操作必须证明来自本站页面：
    - 有 ``Origin``：其 host 必须等于请求 Host，或在
      ``app.config['OPERATOR_ALLOWED_ORIGINS']`` 中（只比 host，兼容 TLS 终止在
      代理、内部看到 http 的部署）；``Origin: null`` 一律拒绝；
    - 无 ``Origin``：``Sec-Fetch-Site`` 为 cross-site/same-site 拒绝；两者都缺
      （curl/脚本等非浏览器客户端）放行——它们不会携带受害者的浏览器身份。
    账户模式由 ``account_boundary`` 负责 Origin，此处不重复判断。
    """
    src = request if req is None else req
    if src.method in _SAFE_METHODS or current_app.config.get("ACCOUNTS_ENABLED"):
        return True
    origin = src.headers.get("Origin")
    if origin is None:
        return src.headers.get("Sec-Fetch-Site", "") not in ("cross-site", "same-site")
    from urllib.parse import urlsplit
    host = urlsplit(origin).netloc.lower()
    if not host:
        return False
    allowed = {src.host.lower()}
    for extra in current_app.config.get("OPERATOR_ALLOWED_ORIGINS") or ():
        extra_host = urlsplit(str(extra)).netloc.lower()
        if extra_host:
            allowed.add(extra_host)
    return host in allowed


def require_operator(view):
    """tasks 域：CF Access 认证（或仅限未携带他域凭据头的 DEV fallback）。
    没有身份 → 401；非账户模式跨站写请求 → 403 ``invalid_origin``。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        email = extract_operator_identity()
        if not email:
            return error_response(
                ApplicationError("unauthorized", "operator identity required", 401))
        if not operator_origin_allowed():
            return error_response(
                ApplicationError("invalid_origin", "请求来源无效", 403))
        g.operator = email
        return view(*args, **kwargs)
    return wrapper


def require_runner(view):
    """commands 域：X-Runner-Credential: <machine>:<secret>，凭据与机器绑定。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        try:
            identity = extract_runner_identity()
        except CredentialStoreUnavailable as exc:
            return _store_unavailable(exc.code)
        if identity is None:
            return _forbidden()
        return view(*args, **kwargs)
    return mark_machine_auth(wrapper)


def require_task_store(view):
    """SQLite 不可用降级：任务 API 跳过，观测链路不受影响（统一错误外型）。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.config.get("TASKS_ENABLED"):
            return error_response(
                ApplicationError("tasks_unavailable", "任务存储不可用，观测链路不受影响", 503))
        return view(*args, **kwargs)
    return wrapper
