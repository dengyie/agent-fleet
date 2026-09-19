"""tools/transport.py — runner/ControlClient 出站 HTTPS 传输（proxy 感知回退）。

问题背景（2026-09-15 定位）：macOS scutil 系统代理（Clash :7897）与 Cloudflare
边缘共同决定出站可达性——同一 URL 直连可能被 CF 1010 拦截，而走系统代理 200。
历史补丁（128a52c）为修「KeepAlive 复用全局 opener 挂在代理隧道握手」把两个
客户端强制成永远直连（``ProxyHandler({})``），在 CF 开始拦直连出口后链路全断。

本模块把出站策略收敛为一个可测的传输函数：

* ``mode="auto"``（默认）：先直连；只有当响应是 *CF 边缘拦截外型*（403 +
  ``error code: 101x`` 文本体，非 hub 的 JSON 错误外型）或传输层异常时，
  下一跳改走系统代理重试。成功后按 sticky 记住（进程内），后续请求直接用
  成功的那条路径，避免每请求都先吃一次握手超时。
* ``mode="direct"`` / ``mode="proxy"``：钉死单路径，行为与旧补丁一致。
* 真实 hub 的 4xx/5xx JSON（401/403/409…）永远原样返回给调用方——
  绝不因业务状态码触发回退，否则会把「凭据错」误判成「路不通」。

设计约束：不引入新依赖；每次尝试新建 opener（不复用全局 urlopen 池，
这是 128a52c 原始教训）；重试/退避节奏仍归各客户端自己管。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

# CF 边缘拦截外型：403 + 纯文本 "error code: 101x"（1010 浏览器指纹 /
# 1020 Access rule 等）。hub 自身错误是 JSON（{"error": ...}），不会命中。
_CF_BLOCK_RE = re.compile(r"error code: 10\d\d", re.IGNORECASE)

_MODES = ("auto", "direct", "proxy")

_TRANSIENT = (TimeoutError, urllib.error.URLError, OSError,
              ConnectionError, BrokenPipeError)


def _is_cf_block(status: int, body: bytes | str) -> bool:
    if status != 403:
        return False
    try:
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) \
            else str(body)
    except Exception:
        return False
    return bool(_CF_BLOCK_RE.search(text))


def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read() or b""
    except Exception:
        return b""


class Transport:
    """可复用的 POST-JSON 出站传输（每个实例持有自己的 sticky 状态）。"""

    def __init__(self, *, mode: str = "auto",
                 timeout: float = 15.0,
                 attempts: int = 3,
                 retry_sleep_s: float = 0.4):
        if mode not in _MODES:
            raise ValueError(f"transport.mode 必须是 {'|'.join(_MODES)}，得到 {mode!r}")
        self._mode = mode
        self._timeout = float(timeout)
        self._attempts = max(1, int(attempts))
        self._retry_sleep_s = float(retry_sleep_s)
        self._sleeper: Callable[[float], None] = time.sleep
        #: sticky: None=未定向; "direct"/"proxy"=上次成功路径（仅 auto 模式使用）
        self._preferred: str | None = None

    # -- 每跳的 opener ----------------------------------------------------

    def _opener(self, use_proxy: bool):
        if use_proxy:
            # 系统代理（macOS = scutil；无代理时 getproxies() 为空 → 等价直连）
            return urllib.request.build_opener(urllib.request.ProxyHandler(
                urllib.request.getproxies()))
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- 单路径一轮 --------------------------------------------------------

    def _open(self, opener, req, timeout):
        """唯一的真实出站出口：一次 attempt 的 urlopen。

        永远走局部 ``opener.open``，绝不调用 ``install_opener`` 改写全局
        状态（128a52c 的多线程竞态教训）。测试替身替换实例的 ``_opener``
        或类级 ``Transport._open`` 注入，生产代码不含任何测试探测分支。
        """
        return opener.open(req, timeout=timeout)

    def _one(self, req: urllib.request.Request, use_proxy: bool,
             timeout: float):
        """单路径一次尝试。返回 (status, payload)；CF 拦截外型抛 _CFBlock。"""
        opener = self._opener(use_proxy)
        try:
            with self._open(opener, req, timeout) as resp:
                raw = resp.read().decode("utf-8") if resp.read else ""
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict):
                    payload = {"_raw": payload}
                # urlopen 惯例：非 HTTPResponse（如测试桩/redirect 处理器）
                # 可能没有 .status —— 缺省 200（成功路径）。
                return int(getattr(resp, "status", 200)), payload
        except urllib.error.HTTPError as exc:
            body = _read_error_body(exc)
            if _is_cf_block(exc.code, body):
                raise _CFBlock(str(exc.code)) from exc
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                payload = {"error": "http_error"}
            if not isinstance(payload, dict):
                payload = {"error": "http_error"}
            return int(exc.code), payload
        # _TRANSIENT 与其它异常一律向上抛，由 _post 统一调度

    # -- 多跳调度 -----------------------------------------------------------

    def _post(self, req: urllib.request.Request, timeout: float):
        """按模式调度路径；auto 模式下 CF 拦截/传输失败可在两路径间切换。

        返回 (status, payload, last_error_str)。status=0 表示两路径均传输失败。
        """
        last_err = ""
        if self._mode == "auto":
            # 路径优先级：sticky > alternate；首选路径失败自动回退到备用路径
            if self._preferred:
                other = "proxy" if self._preferred == "direct" else "direct"
                order = [self._preferred, other]
            else:
                order = ["direct", "proxy"]
        else:
            order = [self._mode]

        for path in order:
            use_proxy = path == "proxy"
            for attempt in range(self._attempts):
                try:
                    status, payload = self._one(req, use_proxy, timeout)
                    if self._mode == "auto" and 200 <= status < 500:
                        self._preferred = path
                    return status, payload, ""
                except _CFBlock:
                    # CF 边缘拦截该路径 → 立即换路径（不打满重试）
                    last_err = "cf_blocked"
                    break
                except _TRANSIENT as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    if attempt + 1 < self._attempts:
                        self._sleeper(self._retry_sleep_s)
                    continue
                except Exception as exc:  # 不可分类：不重试，换/终止路径
                    last_err = f"{type(exc).__name__}: {exc}"
                    break

        if self._mode == "auto":
            self._preferred = None
        return 0, {"error": last_err or "transport_failed"}, last_err

    # -- 公共入口 -----------------------------------------------------------

    def post_json(self, url: str, body: Any | None, headers: dict[str, str],
                  timeout: float | None = None):
        """POST JSON（body=None 时无请求体）。返回 (status, payload)。

        网络层完全失败返回 (0, {"error": ...})；hub 业务 4xx/5xx 原样透传。
        timeout 作为参数透传到每次尝试——不落实例状态（调用方可能跨线程复用）。
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        eff_timeout = self._timeout if timeout is None else float(timeout)
        if eff_timeout <= 0:
            eff_timeout = self._timeout
        status, payload, _ = self._post(req, eff_timeout)
        return status, payload


class _CFBlock(Exception):
    """内部信号：响应匹配 CF 边缘拦截外型。"""
