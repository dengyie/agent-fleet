"""Regression tests for the frontend component escaping contract.

hub/static/components.js renders machine cards by interpolating values into
HTML strings that are assigned to `grid.innerHTML` (via renderFleet in
app.js).  Ingestion payloads are attacker-influenced, so every dynamic value
must be passed through the `esc()` helper.  There is no JS test framework in
this repo, so these tests assert against the component source to guard the
escaping contract without adding a dependency or build chain.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENTS_JS = REPO_ROOT / 'hub' / 'static' / 'components.js'
APP_JS = REPO_ROOT / 'hub' / 'static' / 'app.js'
FRONTEND_DIR = REPO_ROOT / 'frontend'
VIEWS_DIR = FRONTEND_DIR / 'views'

# Payload the Phase 1 review used to demonstrate the stored XSS.
_XSS_PAYLOAD = '<img src=x onerror=alert(1)>'
# Quoted payload must also stay text, never markup.
_QUOTED_PAYLOAD = '" onmouseover="alert(2)" '


class ComponentsXssEscapingTests(unittest.TestCase):
    def test_sys_load_is_escaped_in_render_machine_card(self):
        src = COMPONENTS_JS.read_text()
        # The fixed form must be present: esc() applied, numeric fallback kept.
        self.assertIn('${esc(sys.load) || 0}', src)
        # The raw unescaped interpolation must not reappear.
        self.assertNotIn('${sys.load || 0}', src)

    def test_no_unwrapped_system_field_interpolation_remains(self):
        # Any `${sys.X` token not wrapped in esc() would be an unescaped hole.
        src = COMPONENTS_JS.read_text()
        unwrapped = re.findall(r'\$\{sys\.[A-Za-z_]+', src)
        self.assertEqual(unwrapped, [], f'unescaped system field: {unwrapped}')

    def test_esc_helper_covers_all_html_metacharacters(self):
        src = COMPONENTS_JS.read_text()
        match = re.search(r'function esc\(s\) \{.*?\n\}', src, re.S)
        self.assertIsNotNone(match, 'esc() helper not found in components.js')
        body = match.group(0)
        for escaped in ('&amp;', '&lt;', '&gt;', '&quot;', '&#39;'):
            self.assertIn(escaped, body,
                          f'esc() must escape {escaped}')

    def test_xss_payload_remains_an_attribute_safe_with_esc(self):
        # Verify esc() escapes the trigger characters of the review payload
        # ('<' / '>' break the tag, "'" protects attribute context).
        escaped = (
            _XSS_PAYLOAD
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;')
        )
        self.assertNotIn('<', escaped)
        self.assertNotIn('>', escaped)
        self.assertNotIn("'", escaped)
        self.assertNotIn('"', escaped)


class TaskFixRegressionTests(unittest.TestCase):
    """Task 8 review 回归：fleet 任务数首屏初始化 + 取消/重试按响应门控。"""

    def _task_fn(self, name):
        src = APP_JS.read_text()
        match = re.search(
            r"FleetApp\." + name + r" = function\s*\(.*?\n\};", src, re.S)
        self.assertIsNotNone(match, 'FleetApp.' + name + ' not found')
        return match.group(0)

    def test_fleet_badge_initializes_on_first_load(self):
        src = APP_JS.read_text()
        init = re.search(
            r"document\.addEventListener\('DOMContentLoaded'.*?\n\}\);",
            src, re.S)
        self.assertIsNotNone(init, 'DOMContentLoaded init block not found')
        body = init.group(0)
        self.assertIn("page === 'fleet'", body, 'fleet 分支缺失')
        self.assertIn('refreshTaskCount()', body, '未拉取真实任务数')
        # 只做一次性调用，不得在 init 内建立无限请求循环
        self.assertNotIn('setInterval', body, 'init 内不得轮询')

    def test_cancel_gated_on_response_ok(self):
        body = self._task_fn('cancelTask')
        self.assertIn("taskActionError('')", body, '开始前应清空错误')
        self.assertIn('r.status', body, '应解析响应状态')
        self.assertIn('if (res.d.ok)', body, '成功门控缺失')
        self.assertIn('taskActionError', body, '失败应显示可见错误')
        ok_idx = body.find('if (res.d.ok)')
        reload_idx = body.find('location.reload()')
        self.assertNotEqual(reload_idx, -1, '成功路径应有 reload')
        self.assertGreater(reload_idx, ok_idx, 'reload 必须被 ok 守卫')
        # 守卫之前（函数体前段）不得出现 reload
        self.assertNotIn('location.reload()', body[:ok_idx],
                         'reload 出现在 ok 守卫之前')

    def test_retry_gated_on_response_ok(self):
        src = APP_JS.read_text()
        match = re.search(
            r"FleetApp\.retryTask = function\s*\(.*?\n\};", src, re.S)
        self.assertIsNotNone(match, 'FleetApp.retryTask not found')
        body = match.group(0)
        self.assertIn("taskActionError('')", body)
        self.assertIn('r.status', body)
        self.assertIn('if (res.d.ok)', body)
        ok_idx = body.find('if (res.d.ok)')
        reload_idx = body.find('location.reload()')
        self.assertNotEqual(reload_idx, -1)
        self.assertGreater(reload_idx, ok_idx, 'reload 必须被 ok 守卫')
        prefix = body[:ok_idx]
        self.assertNotIn('location.reload()', prefix, '条件前不得 reload')
        # 失败分支应显示可见错误
        self.assertIn('taskActionError(res.d.detail || res.d.error ||', body)

    def test_task_action_errors_use_safe_textcontent(self):
        src = APP_JS.read_text()
        self.assertIn('el.textContent = msg', src, '错误应用 textContent 写入')


class ViewXssProtectionTests(unittest.TestCase):
    """Task 15: standalone views (fleet.js / machine.js) must be DOM-safe.

    The new views render attacker-influenced observation/task values, so no
    dynamic value may ever be written through innerHTML, insertAdjacentHTML,
    document.write or string-concatenated markup.  The synthetic ``<img>`` and
    quoted payloads must remain text because every dynamic write goes through
    ``textContent`` / ``createTextNode`` (the browser auto-escapes there).
    """

    def _view_sources(self):
        return {
            p.name: p.read_text()
            for p in VIEWS_DIR.glob('*.js') if p.is_file()
        }

    def test_views_never_write_dynamic_markup(self):
        sources = self._view_sources()
        for name, source in sources.items():
            self.assertNotIn('innerHTML', source, name)
            self.assertNotIn('insertAdjacentHTML', source, name)
            self.assertNotIn('outerHTML', source, name)
            self.assertNotIn('document.write', source, name)

    def test_views_write_dynamic_text_via_textcontent(self):
        # 每个视图都必须包含 textContent 写入路径（浏览器自动转义）。
        sources = self._view_sources()
        for name, source in sources.items():
            self.assertIn('textContent', source, name)
            self.assertIn('createElement', source, name)

    def test_views_do_not_build_html_strings_from_dynamic_values(self):
        # 视图不得用字符串拼接构造 HTML 片段（反引号模板 / join 标签均禁止）。
        for name, source in self._view_sources().items():
            self.assertNotIn('${', source, name)
            self.assertNotIn(".join('')", source, name)

    def test_machine_form_error_uses_textcontent_only(self):
        machine = (VIEWS_DIR / 'machine.js').read_text()
        self.assertIn('errorBox.textContent', machine)
        self.assertNotIn('errorBox.innerHTML', machine)
        self.assertIn("pagePath('task', task.task_id)", machine)

    def test_task_view_renders_patch_and_test_names_via_textcontent(self):
        task = (VIEWS_DIR / 'task.js').read_text()
        self.assertIn('pre.textContent', task)
        self.assertIn('line.textContent', task)
        self.assertIn('test_summary', task)
        self.assertIn('failed_names', task)
        self.assertNotIn('innerHTML', task)

    def test_payloads_remain_text_under_textcontent(self):
        # 与 Phase 1 回归一致：经 textContent 写入时 <img> 与引号攻击载荷
        # 都只是字符串，绝不会变成元素/属性。视图只用 textContent / 建元素
        # 写入，所以任何动态值都不会被当作 HTML/属性解析。
        self.assertIn('<', _XSS_PAYLOAD)
        self.assertIn('>', _XSS_PAYLOAD)
        self.assertIn('"', _QUOTED_PAYLOAD)
        for name, source in self._view_sources().items():
            # 不存在任何把动态文本以 HTML 传播的写入点
            self.assertNotIn('.innerHTML', source, name)
            self.assertNotIn('.outerHTML', source, name)
            self.assertNotIn('document.write', source, name)


if __name__ == '__main__':
    unittest.main()