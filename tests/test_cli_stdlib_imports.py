"""Run actual CLI entrypoints without the pytest process's preloaded stdlib."""
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_probe_direct_script_collects_from_unrelated_cwd(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT/'tools/agent-self-report.py'),
                             '--name','fixture','--agents','generic','--dry-run'],
                            cwd=tmp_path, capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['system']['platform']


def test_runner_direct_bootstrap_preserves_stdlib_and_package(tmp_path):
    code = '''import runpy,sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
runpy.run_path(sys.argv[1], run_name='direct_import_fixture')
import platform, tools.platform
assert callable(platform.system)
assert platform is not tools.platform
'''
    result=subprocess.run([sys.executable,'-c',code,str(ROOT/'tools/agent-runner.py')],
                          cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr


def test_probe_bootstraps_before_importing_uuid(tmp_path):
    code = '''import builtins,runpy,sys
from pathlib import Path
script=Path(sys.argv[1])
sys.path.insert(0,str(script.parent))
original_import=builtins.__import__
def guarded_import(name,*args,**kwargs):
    if name == "uuid":
        assert str(script.parent) not in sys.path, "uuid imported before bootstrap"
    return original_import(name,*args,**kwargs)
builtins.__import__=guarded_import
sys.argv=[str(script),"--help"]
runpy.run_path(str(script),run_name="__main__")
'''
    result=subprocess.run([sys.executable,'-c',code,str(ROOT/'tools/agent-self-report.py')],
                          cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
