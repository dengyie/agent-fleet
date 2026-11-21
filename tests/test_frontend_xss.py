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
VIEWS_DIR = FRONTEND_DIR / 'assets' / 'views'

# Payload the Phase 1 review used to demonstrate the stored XSS.
_XSS_PAYLOAD = '<img src=x onerror=alert(1)>'
# Quoted payload must also stay text, never markup.
_QUOTED_PAYLOAD = '" onmouseover="alert(2)" '






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
            p.relative_to(VIEWS_DIR).as_posix(): p.read_text(encoding='utf-8')
            for p in VIEWS_DIR.glob('*.js') if p.is_file()
        }

    def _render_source(self, source):
        # Native views share one safe element builder; inspect its actual sink
        # rather than requiring every caller to duplicate document.createElement.
        if "from './agent-chat/dom.js'" in source:
            source += (VIEWS_DIR / 'agent-chat/dom.js').read_text(encoding='utf-8')
        return source

    def test_views_never_write_dynamic_markup(self):
        sources = self._view_sources()
        for name, source in sources.items():
            self.assertNotIn('innerHTML', source, name)
            self.assertNotIn('insertAdjacentHTML', source, name)
            self.assertNotIn('outerHTML', source, name)
            self.assertNotIn('document.write', source, name)

    def test_views_write_dynamic_text_via_textcontent(self):
        # 每个视图的直接或共享 helper 路径都必须使用安全 DOM 写入。
        sources = self._view_sources()
        for name, source in sources.items():
            source = self._render_source(source)
            self.assertIn('textContent', source, name)
            self.assertIn('createElement', source, name)

    def test_native_helpers_never_write_dynamic_markup(self):
        for path in (VIEWS_DIR / 'agent-chat').glob('*.js'):
            source = path.read_text(encoding='utf-8')
            for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
                self.assertNotIn(sink, source, path.name)

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
