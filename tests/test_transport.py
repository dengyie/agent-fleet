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
    def test_cf_block_exception_carries_status(self):
        exc = _http_error(403, b"error code: 1010\n")
        with self.assertRaises(_CFBlock):
            body = exc.read()
            if not transport_mod._is_cf_block(exc.code, body):
                raise AssertionError("should match")
            raise _CFBlock(str(exc.code))


if __name__ == "__main__":
    unittest.main()
