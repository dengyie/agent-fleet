"""Optional static-release hosting; no template, business or persistence imports.

Production can serve frontend/ directly through the edge and start the Hub with
--no-serve-frontend. Both hosts serve identical bytes and URL paths.
"""
from pathlib import Path
from flask import Blueprint, abort, current_app, send_file

bp = Blueprint('pages', __name__)


def _file(relative: str, *, asset: bool = False):
    if not current_app.config.get('SERVE_FRONTEND', True):
        abort(404)
    configured = current_app.config.get('FRONTEND_DIR')
    if not configured or any(part in ('.', '..') for part in relative.split('/')):
        abort(404)
    try:
        root = Path(configured).resolve()
        boundary = (root / 'assets').resolve() if asset else root
        # assets itself must not be a symlink to an external directory.
        boundary.relative_to(root)
        target = (root / relative).resolve()
        target.relative_to(boundary)
        if not target.is_file():
            abort(404)
        response = send_file(target)
        # Unhashed module filenames must revalidate across independent releases.
        response.headers['Cache-Control'] = 'no-cache'
        return response
    except (OSError, ValueError):
        abort(404)


@bp.route('/')
@bp.route('/index.html')
@bp.route('/login')
@bp.route('/assistant')
@bp.route('/monitoring')
@bp.route('/machine/<entity>')
@bp.route('/task/<entity>')
@bp.route('/session/<entity>')
@bp.route('/conversation/<entity>')
def index(entity=None):
    return _file('index.html')


@bp.route('/config.js')
def configuration():
    return _file('config.js')


@bp.route('/assets/<path:filename>')
def asset(filename):
    if Path(filename).suffix not in {'.js', '.css', '.svg', '.png', '.woff2'}:
        abort(404)
    return _file('assets/' + filename, asset=True)
