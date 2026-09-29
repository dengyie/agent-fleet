import subprocess

import pytest

from tools.result_files import redact_patch


@pytest.mark.parametrize('deleting', [False, True])
def test_redacted_patch_preserves_git_null_device_and_applies(tmp_path, deleting):
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    if deleting:
        (tmp_path/'hello.txt').write_text('hi\n')
    patch = ('diff --git a/hello.txt b/hello.txt\n' +
        ('deleted file mode 100644\n--- a/hello.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-hi\n' if deleting else
         'new file mode 100644\n--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hi\n'))
    clean, changed = redact_patch(patch)
    assert clean == patch and not changed
    assert redact_patch(clean) == (clean, False)
    subprocess.run(['git','apply','-'],cwd=tmp_path,input=clean,text=True,check=True)
    assert not (tmp_path/'hello.txt').exists() if deleting else (tmp_path/'hello.txt').read_text()=='hi\n'


def test_patch_header_exception_does_not_disable_body_redaction():
    patch = '--- /dev/null\n+++ b/config.txt\n@@ -0,0 +1 @@\n+api_key=sk-test-very-private-secret-value\n'
    clean, changed = redact_patch(patch)
    assert '--- /dev/null' in clean
    assert 'sk-test-very-private-secret-value' not in clean
    assert changed
