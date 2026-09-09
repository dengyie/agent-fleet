"""tests/test_task_view_review.py — Task 16 复查：任务视图 / 兼容 shell cutover

聚焦 Task 16 final review fixes，不重写既有的 1100 行 frontend 契约测试：
  - 前端 task.js 源码契约：加载代际栅栏、终态 class 白名单、命名日志上界
  - 前端 cutover 配置：默认关闭、暴露 frontend_dir、应用装配兼容
  - cutover 开启时 / /machine/<n> /task/<id> 返回静态 shell + root base href
  - cutover 关闭时旧模板/状态行为保持不变
  - shell 缺失失败关闭为有界非敏感响应
"""
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app
from hub.config import FleetConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

_TASK_JS = None


def _task_js():
    global _TASK_JS
    if _TASK_JS is None:
        _TASK_JS = FRONTEND_DIR.joinpath("views", "task.js").read_text(encoding="utf-8")
    return _TASK_JS


class TaskJsSourceContractTests(unittest.TestCase):
    """前端 task.js 源码级契约（无需 Node 运行时）。"""

    def test_load_task_uses_generation_fencing(self):
        source = _task_js()
        # 请求代际单调递增：重试发起新 loadTask 时旧请求失效
        self.assertIn("++loadSeq", source)
        # 成功与失败回调都必须先校验代际再写状态（两处）
        fail_branch = source.split("function loadTask()", 1)[1]
        self.assertEqual(fail_branch.count("generation !== loadSeq"), 2)
        # 卸载栅栏保留
        self.assertIn("if (disposed", source)
        self.assertIn("disposed = true", source)

    def test_terminal_class_is_allowlisted_not_concatenated(self):
        source = _task_js()
        # 终止节点 class 来自显式白名单映射
        self.assertIn("var TERMINAL_CLASS", source)
        field = source.split("var TERMINAL_CLASS", 1)[1].split("};", 1)[0]
        for cls in ("terminal-failed", "terminal-cancelled", "terminal-expired"):
            self.assertIn(cls, field)
        # 渲染使用白名单解析器，而非把状态名拼进 class 串
        self.assertIn("terminalClass(state)", source)
        self.assertNotIn("terminal-' + state", source)

    def test_log_summary_bound_is_named(self):
        source = _task_js()
        self.assertIn("var MAX_LOG_SUMMARY_RENDER", source)
        render_slice = source.split("function renderLogs", 1)[1]
        # 渲染处使用命名常量而非硬编码 4096
        self.assertIn("slice(0, MAX_LOG_SUMMARY_RENDER)", render_slice)
        self.assertNotIn("slice(0, 4096)", render_slice)


class FrontendCutoverConfigTests(unittest.TestCase):
    def test_cutover_defaults_off(self):
        root = Path(tempfile.mkdtemp())
        cfg = FleetConfig.from_root(root)
        self.assertFalse(cfg.frontend_cutover)
        # 仓库本地 frontend 目录默认派生
        self.assertEqual(cfg.frontend_dir, root / "frontend")

    def test_cutover_accepts_explicit_values(self):
        root = Path(tempfile.mkdtemp())
        custom = Path(tempfile.mkdtemp()) / "shell"
        cfg = FleetConfig.from_root(root, frontend_cutover=True, frontend_dir=custom)
        self.assertTrue(cfg.frontend_cutover)
        self.assertEqual(cfg.frontend_dir, custom)

    def test_legacy_make_app_keeps_cutover_off_and_dir_wired(self):
        # web.make_app（legacy 调用点）不传 cutover 参数时仍默认关闭，
        # 且 FRONTEND_DIR 已接好（默认仓库内 frontend/）。
        from hub import web as web_module

        old_state, old_event = store.STATE_DIR, events.EVENT_LOG
        try:
            with tempfile.TemporaryDirectory() as d:
                store.STATE_DIR = Path(d)
                events.EVENT_LOG = Path(d) / "events.jsonl"
                app = web_module.make_app(ingest_token="secret")
        finally:
            store.STATE_DIR, events.EVENT_LOG = old_state, old_event
        self.assertFalse(app.config["FRONTEND_CUTOVER"])
        self.assertEqual(Path(app.config["FRONTEND_DIR"]), REPO_ROOT / "frontend")

    def test_legacy_make_app_accepts_cutover_values(self):
        from hub import web as web_module

        old_state, old_event = store.STATE_DIR, events.EVENT_LOG
        try:
            with tempfile.TemporaryDirectory() as d:
                store.STATE_DIR = Path(d)
                events.EVENT_LOG = Path(d) / "events.jsonl"
                custom = Path(d) / "frontend-release"
                app = web_module.make_app(
                    ingest_token="secret",
                    frontend_cutover=True,
                    frontend_dir=custom,
                )
        finally:
            store.STATE_DIR, events.EVENT_LOG = old_state, old_event
        self.assertTrue(app.config["FRONTEND_CUTOVER"])
        self.assertEqual(Path(app.config["FRONTEND_DIR"]), custom)


class _CutoverBase(unittest.TestCase):
    """cutover 集成基类：临时存储 + 自建 shell 目录。"""

    cutover = False

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_state = store.STATE_DIR
        self.old_event = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.temp_dir
        events.EVENT_LOG = self.temp_dir / "events.jsonl"
        task_store.DB_PATH = self.temp_dir / "fleet.db"
        task_store.init_db()
        store.save_snapshot("hk", {"machine": "hk", "source": "ingest",
                                   "reachable": True, "agents": {}, "system": {}})
        self.shell_dir = Path(tempfile.mkdtemp()) / "frontend"
        self.shell_dir.mkdir(parents=True)
        self.shell_dir.joinpath("index.html").write_text(
            "<!DOCTYPE html><html><head><title>shell</title></head>"
            "<body data-page='fleet'></body></html>", encoding="utf-8")
        self.app = create_app(self._config())
        self.client = self.app.test_client()

    def tearDown(self):
        store.STATE_DIR = self.old_state
        events.EVENT_LOG = self.old_event
        task_store.DB_PATH = self.old_db

    def _config(self):
        return FleetConfig.from_root(
            self.temp_dir,
            ingest_token="secret",
            dev_operator="op@example.com",
            frontend_cutover=self.cutover,
            frontend_dir=self.shell_dir,
        )


class CutoverEnabledShellTests(_CutoverBase):
    cutover = True

    def test_index_serves_shell_with_root_base(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("<title>shell</title>", html)
        self.assertIn('<base href="/">', html)

    def test_machine_route_serves_shell(self):
        resp = self.client.get("/machine/hk")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<base href="/">', resp.get_data(as_text=True))

    def test_task_route_serves_shell_even_for_unknown_task(self):
        resp = self.client.get("/task/t-unknown")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<base href="/">', resp.get_data(as_text=True))

    def test_nested_paths_resolve_assets_via_origin_root_base(self):
        # 注入 origin-root base href 后，嵌套路径下相对资源解析到站点根
        html = self.client.get("/machine/hk").get_data(as_text=True)
        self.assertIn('<base href="/">', html)

    def test_shell_carries_no_business_data_or_secrets(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("secret", html)
        self.assertNotIn("ingest", html)
        self.assertNotIn("Traceback", html)


class CutoverDisabledCompatibilityTests(_CutoverBase):
    cutover = False

    def test_fleet_page_uses_legacy_template(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-page="fleet"', html)
        self.assertIn("static/style.css", html)
        self.assertNotIn('<base href="/">', html)

    def test_machine_page_legacy_404_for_unknown(self):
        self.assertEqual(self.client.get("/machine/ghost").status_code, 404)

    def test_task_page_legacy_404_for_unknown(self):
        resp = self.client.get("/task/t-unknown")
        self.assertIn(resp.status_code, (404, 503))


class CutoverMissingShellTests(_CutoverBase):
    """shell 缺失时失败关闭：有界非敏感响应，且不落到旧模板。"""

    cutover = True

    def setUp(self):
        super().setUp()
        # 覆盖为不存在的 shell 目录
        missing = self.temp_dir / "no-such-frontend"
        self.app = self.app_config_store = None
        self.app = create_app(
            FleetConfig.from_root(
                self.temp_dir,
                ingest_token="secret",
                dev_operator="op@example.com",
                frontend_cutover=True,
                frontend_dir=missing,
            )
        )
        self.client = self.app.test_client()

    def test_missing_shell_fails_closed_bounded_non_secret(self):
        for path in ("/", "/machine/hk", "/task/any"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.get_data(as_text=True), "frontend shell unavailable")
            body = resp.get_data(as_text=True)
            # 不泄露路径 / 异常细节
            self.assertNotIn("no-such-frontend", body)
            self.assertNotIn("Traceback", body)


class _RealFrontendCutoverBase(_CutoverBase):
    """Cutover base using the real repository frontend/ directory."""

    def _config(self):
        return FleetConfig.from_root(
            self.temp_dir,
            ingest_token="secret",
            dev_operator="op@example.com",
            frontend_cutover=self.cutover,
            frontend_dir=REPO_ROOT / "frontend",
        )


class CutoverEnabledAssetServingTests(_RealFrontendCutoverBase):
    """Cutover mode serves static frontend assets with correct MIME types."""

    cutover = True

    def test_serves_config_js(self):
        resp = self.client.get("/config.js")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/javascript")
        self.assertIn("apiBaseUrl", resp.get_data(as_text=True))

    def test_serves_styles_app_css(self):
        resp = self.client.get("/styles/app.css")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/css")
        self.assertIn("--bg: #0f172a", resp.get_data(as_text=True))

    def test_serves_frontend_api_client_js(self):
        """Frontend api/client.js is served as a static asset under /api/."""
        resp = self.client.get("/api/client.js")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/javascript")
        self.assertIn("HTTP 入口", resp.get_data(as_text=True))

    def test_serves_view_module(self):
        resp = self.client.get("/views/fleet.js")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/javascript")
        self.assertIn("Fleet 总览视图", resp.get_data(as_text=True))

    def test_traversal_outside_frontend_dir_returns_404(self):
        resp = self.client.get("/../../../etc/passwd")
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_asset_returns_404(self):
        resp = self.client.get("/nonexistent.js")
        self.assertEqual(resp.status_code, 404)

    def test_out_of_release_path_404_even_if_file_exists(self):
        """A file under FRONTEND_DIR but outside the release-layout prefixes
        (e.g. /secret.txt) is not served even when the file exists."""
        with tempfile.TemporaryDirectory() as d:
            asset_dir = Path(d)
            asset_dir.joinpath("secret.txt").write_text(
                "top-secret", encoding="utf-8")
            app = create_app(
                FleetConfig.from_root(
                    self.temp_dir,
                    ingest_token="secret",
                    dev_operator="op@example.com",
                    frontend_cutover=True,
                    frontend_dir=asset_dir,
                )
            )
            resp = app.test_client().get("/secret.txt")
            self.assertEqual(resp.status_code, 404)

    def test_allowlist_prefix_traversal_resolves_to_404(self):
        """api/../secret.txt (or styles/../secret.txt) passes the allowlist
        prefix but must NOT resolve to an out-of-release file inside
        FRONTEND_DIR — explicit `..`/`.` segment rejection keeps it 404."""
        with tempfile.TemporaryDirectory() as d:
            asset_dir = Path(d)
            asset_dir.joinpath("secret.txt").write_text(
                "top-secret", encoding="utf-8")
            app = create_app(
                FleetConfig.from_root(
                    self.temp_dir,
                    ingest_token="secret",
                    dev_operator="op@example.com",
                    frontend_cutover=True,
                    frontend_dir=asset_dir,
                )
            )
            client = app.test_client()
            for path in ("/api/../secret.txt", "/styles/../secret.txt"):
                self.assertEqual(client.get(path).status_code, 404)

    def test_unknown_api_path_goes_through_existing_json_handler(self):
        """Unknown /api/* paths hit the existing API JSON 404 handler, not
        the asset handler, even when cutover is enabled."""
        resp = self.client.get("/api/unknown")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("ok", resp.get_data(as_text=True))


class CutoverDisabledAssetNotServedTests(_RealFrontendCutoverBase):
    """Cutover disabled does not serve frontend static assets."""

    cutover = False

    def test_disabled_does_not_serve_config_js(self):
        resp = self.client.get("/config.js")
        self.assertEqual(resp.status_code, 404)
        # Plain-text 404 from the page-style error handler (not API JSON).
        self.assertNotIn("ok", resp.get_data(as_text=True))

    def test_disabled_does_not_serve_styles_css(self):
        resp = self.client.get("/styles/app.css")
        self.assertEqual(resp.status_code, 404)

    def test_disabled_does_not_serve_frontend_api_js(self):
        """api/client.js exists in frontend/ but must not be served."""
        resp = self.client.get("/api/client.js")
        self.assertEqual(resp.status_code, 404)
        # /api/ prefix triggers the JSON 404 handler even when the
        # catch-all is disabled.
        self.assertIn("ok", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()