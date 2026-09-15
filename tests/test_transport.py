"""tests/test_transport.py — tools/transport.py 出站传输（proxy 感知回退）。

覆盖 2026-09-15 卡点定位报告的修复契约：
- auto 模式：直连被 CF 1010 拦截 → 回退系统代理 → 成功；
- 真实 hub 的 403 JSON（无 operator / 凭据拒绝）绝不触发回退；
- sticky：首跳成功后记住路径，后续请求不再先吃一次失败；
- pinned direct/proxy 模式：行为与旧 ProxyHandler({}) 补丁一致；
- 传输层异常重试节奏与 attempts 上限；
- mode 非法值 fail-fast。
"""
from __future__ import annotations

import json
import time
import unittest
import urllib.error
from unittest import mock

from tools import transport as transport_mod
from tools.transport import Transport, _CFBlock, _is_cf_block


def _http_error(status, body=b'{"error": "forbidden"}'):
    class _Fp:
        def read(self):
            return body

        def close(self):
            return None

    return urllib.error.HTTPError("https://hub.test/x", status, "Forbidden",
                                  hdrs=None, fp=_Fp())


class _Resp:
    def __init__(self, status=200, payload=None, raw=None):
        self.status = status
        self._raw = raw if raw is not None else json.dumps(payload or {}).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpeners:
    """按 (use_proxy, call_index) 路由的 opener 工厂替身。

    direct_open / proxy_open: callable(req, timeout) -> _Resp 或 raise。
    """

    def __init__(self, direct_open, proxy_open):
        self.direct_open = direct_open
        self.proxy_open = proxy_open
        self.direct_calls = 0
        self.proxy_calls = 0

    def install(self, instance: Transport, patcher: "ModulePatcher"):
        def fake_opener(use_proxy):
            fn = self.proxy_open if use_proxy else self.direct_open

            def call(req, timeout):
                if use_proxy:
                    self.proxy_calls += 1
                else:
                    self.direct_calls += 1
                return fn(req, timeout)
            return mock.Mock(open=call)
        instance._opener = fake_opener  # noqa: SLF001 - 测试桩
        patcher.install_opener_target(instance)


class ModulePatcher:
    """把 transport 模块内的 ``urllib.request`` 替换为可控替身。

    _one 通过 ``urllib.request.install_opener`` + ``urllib.request.urlopen``
    走出站（与旧生产代码语义一致）；替身只保留这两个入口，urlopen 转发给
    当前已安装的 opener。
    """

    def __init__(self):
        self._installed_opener = None

    def install_opener_target(self, instance: Transport):
        import tools.transport as tm

        class _UR:
            Request = staticmethod(urllib.request.Request)
            ProxyHandler = urllib.request.ProxyHandler
            build_opener = staticmethod(urllib.request.build_opener)
            URLError = urllib.error.URLError

            @staticmethod
            def install_opener(opener):
                ModulePatcher.current._installed_opener = opener

            @staticmethod
            def urlopen(req, timeout):
                opener = ModulePatcher.current._installed_opener
                if opener is None:
                    raise OSError("no installed opener (test stub)")
                return opener.open(req, timeout=timeout)

        ModulePatcher.current = self
        tm.urllib.request = _UR()

    def restore(self):
        import tools.transport as tm
        tm.urllib.request = urllib.request


def _make(mode="auto", **kw):
    t = Transport(mode=mode, **kw)
    t._sleeper = lambda s: None
    return t


_MODULE_PATCHER = ModulePatcher()


def setUpModule():
    _MODULE_PATCHER.install_opener_target(None)


def tearDownModule():
    _MODULE_PATCHER.restore()


class CFBlockDetectionTests(unittest.TestCase):
    def test_403_error_code_1010_is_cf_block(self):
        self.assertTrue(_is_cf_block(403, b"error code: 1010\n"))
        self.assertTrue(_is_cf_block(403, "error code: 1020"))

    def test_real_hub_403_json_is_not_cf_block(self):
        self.assertFalse(_is_cf_block(403, b'{"error": "forbidden"}'))
        self.assertFalse(_is_cf_block(401, b"error code: 1010"))  # 非 403 不算
        self.assertFalse(_is_cf_block(200, b"ok"))


class AutoFallbackTests(unittest.TestCase):
    def test_direct_cf_block_falls_back_to_system_proxy(self):
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                _http_error(403, b"error code: 1010\n")),
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True, "commands": []}),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json(
            "https://hub.test/api/supervisor/poll", None,
            {"X-Supervisor-Credential": "m:s"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(openers.direct_calls, 1)
        self.assertEqual(openers.proxy_calls, 1)

    def test_direct_transport_error_falls_back_to_proxy(self):
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                OSError("SSL: UNEXPECTED_EOF_WHILE_READING")),
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json("https://hub.test/api/commands/poll", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(openers.proxy_calls, 1)

    def test_hub_business_403_json_never_falls_back(self):
        # 真实 hub 的 403 JSON（无 operator / 凭据拒绝）必须原样透传，
        # 绝不能被当成「路不通」去切换代理路径。
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                _http_error(403, b'{"error": "forbidden"}')),
            proxy_open=lambda req, timeout: (_ for _ in ()).throw(AssertionError(
                "must not reach proxy for a hub business 403")),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json("https://hub.test/api/tasks", {}, {})
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "forbidden")
        self.assertEqual(openers.proxy_calls, 0)

    def test_both_paths_fail_returns_zero_status(self):
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                OSError("no route")),
            proxy_open=lambda req, timeout: (_ for _ in ()).throw(
                OSError("no route either")),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json("https://hub.test/x", None, {})
        self.assertEqual(status, 0)
        self.assertIn("error", payload)

    def test_sticky_remembered_after_proxy_success(self):
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                _http_error(403, b"error code: 1010\n")),
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        t.post_json("https://hub.test/x", None, {})
        self.assertEqual(t._preferred, "proxy")
        # 第二跳：direct 不应再被尝试
        t.post_json("https://hub.test/x", None, {})
        self.assertEqual(openers.direct_calls, 1)
        self.assertEqual(openers.proxy_calls, 2)

    def test_retry_within_path_before_switching(self):
        # 同一路径内先吃满 attempts 次传输重试，再换路径
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                TimeoutError("handshake")),
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        sleeps = []
        t._sleeper = lambda s: sleeps.append(s)
        status, _ = t.post_json("https://hub.test/x", None, {})
        self.assertEqual(status, 200)
        self.assertEqual(openers.direct_calls, 3)  # attempts=3
        self.assertEqual(len(sleeps), 2)  # 3 次尝试之间 2 次 sleep


class PinnedModeTests(unittest.TestCase):
    def test_pinned_direct_never_touches_proxy(self):
        t = _make(mode="direct")
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                OSError("down")),
            proxy_open=lambda req, timeout: (_ for _ in ()).throw(
                AssertionError("pinned direct must not fall back")),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json("https://hub.test/x", None, {})
        self.assertEqual(status, 0)
        self.assertEqual(openers.proxy_calls, 0)

    def test_pinned_proxy_never_touches_direct(self):
        t = _make(mode="proxy")
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                AssertionError("pinned proxy must not touch direct")),
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json("https://hub.test/x", None, {})
        self.assertEqual(status, 200)
        self.assertEqual(openers.direct_calls, 0)


class ValidationTests(unittest.TestCase):
    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            Transport(mode="magic")

    def test_post_json_body_none_has_no_data(self):
        t = _make()
        captured = {}

        def open_fn(req, timeout):
            captured["data"] = req.data
            return _Resp(200, {"ok": True})

        openers = _FakeOpeners(open_fn, open_fn)
        openers.install(t, _MODULE_PATCHER)
        t.post_json("https://hub.test/x", None, {"X-Test": "1"})
        self.assertIsNone(captured["data"])


class CFBlockSignalTests(unittest.TestCase):
    def test_cf_block_via_real_dispatch_path(self):
        """403+1010 经 _post 真实调度路径识别（非手动 raise 的同义反复）。"""
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                _http_error(403, b"error code: 1010\n")),
            proxy_open=lambda req, timeout: (_ for _ in ()).throw(
                _http_error(403, b"error code: 1010\n")),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json(
            "https://hub.test/api/supervisor/poll", None, {})
        # 两路径都被 CF 拦截 → 不重试打满，直接 status=0 + cf_blocked
        self.assertEqual(status, 0)
        self.assertEqual(payload["error"], "cf_blocked")
        self.assertEqual(openers.direct_calls, 1)
        self.assertEqual(openers.proxy_calls, 1)

    def test_default_sleeper_is_time_sleep(self):
        """防回退：生产默认构造必须绑真实 sleep（重试节奏存在）。"""
        t = Transport(mode="auto")
        self.assertIs(t._sleeper, time.sleep)


class TimeoutOverrideTests(unittest.TestCase):
    """post_json(timeout=) 必须参数透传，不改实例状态（跨线程安全）。"""

    def test_timeout_kwarg_reaches_attempt_without_mutating_instance(self):
        t = _make()
        seen = []
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (seen.append(timeout),
                                              _Resp(200, {"ok": True}))[1],
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        status, _ = t.post_json(
            "https://hub.test/api/poll", None, {}, timeout=30)
        self.assertEqual(status, 200)
        self.assertEqual(seen, [30])
        self.assertEqual(t._timeout, _make()._timeout)  # 实例态未被污染

    def test_timeout_default_uses_instance_value(self):
        t = _make(timeout=7)
        seen = []
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (seen.append(timeout),
                                              _Resp(200, {"ok": True}))[1],
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        t.post_json("https://hub.test/api/poll", None, {})
        self.assertEqual(seen, [7])


class Hub5xxPassthroughTests(unittest.TestCase):
    """hub 业务 5xx JSON（非 CF 外型）必须原样透传，绝不触发路径回退。"""

    def test_503_json_not_retried_across_paths(self):
        t = _make()
        openers = _FakeOpeners(
            direct_open=lambda req, timeout: (_ for _ in ()).throw(
                _http_error(503, b'{"error": "unavailable"}')),
            proxy_open=lambda req, timeout: _Resp(200, {"ok": True}),
        )
        openers.install(t, _MODULE_PATCHER)
        status, payload = t.post_json(
            "https://hub.test/api/supervisor/poll", None, {})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "unavailable")
        self.assertEqual(openers.proxy_calls, 0)  # 不回退


class OpenerProxyHandlerTests(unittest.TestCase):
    """_opener(use_proxy=True) 安装的 handler 必须携带系统代理。"""

    def test_proxy_opener_uses_getproxies(self):
        t = Transport(mode="auto")
        with mock.patch.object(transport_mod.urllib.request, "getproxies",
                               return_value={"https": "http://127.0.0.1:7897"},
                               create=True):
            opener = t._opener(use_proxy=True)
        handlers = [h for h in opener.handlers
                    if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].proxies.get("https"),
                         "http://127.0.0.1:7897")

    def test_direct_opener_has_empty_proxies(self):
        # urllib.build_opener 会丢弃空 proxies 的 ProxyHandler（无代理可设），
        # 因此「直连」的正确断言是：不出现任何带代理值的 ProxyHandler。
        t = Transport(mode="auto")
        opener = t._opener(use_proxy=False)
        handlers = [h for h in opener.handlers
                    if isinstance(h, urllib.request.ProxyHandler)]
        empty = all(not h.proxies for h in handlers)
        self.assertTrue(empty)


if __name__ == "__main__":
    unittest.main()
