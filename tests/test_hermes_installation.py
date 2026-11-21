import json

from connectors.hermes import HermesConnector


def test_skills_only_directory_is_not_an_installed_agent(tmp_path, monkeypatch):
    root=tmp_path/'.hermes/skills'
    root.mkdir(parents=True)
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('USERPROFILE',str(tmp_path))
    monkeypatch.setattr('shutil.which',lambda name: None)
    class Context:
        def run_python(self,script):
            from contextlib import redirect_stdout
            from io import StringIO
            output=StringIO()
            try:
                with redirect_stdout(output):
                    exec(script,{'__name__':'__main__'})
            except SystemExit:
                pass
            return output.getvalue()
    assert not HermesConnector().detect(Context())
    assert HermesConnector().collect(Context())=={'installed':False}


def test_hermes_detection_uses_python_on_windows_without_posix_shell():
    class Context:
        def run(self,*args):
            raise AssertionError('must not need bash')
        def run_python(self,script):
            return json.dumps({'installed':True})
    assert HermesConnector().detect(Context())
