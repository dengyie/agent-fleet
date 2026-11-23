"""Observe Antigravity installation without starting its engine or reading auth."""
import json

from .base import BaseConnector


class AgyConnector(BaseConnector):
    TYPE = 'agy'

    def collect(self, ctx):
        raw = ctx.run_python(
            'import json,shutil;print(json.dumps({"installed":bool(shutil.which("agy"))}))')
        try:
            value = json.loads(raw)
            if type(value.get('installed')) is bool:
                return {'installed': value['installed']}
        except (ValueError, TypeError, AttributeError):
            pass
        return {'error': 'installation_unknown'}
