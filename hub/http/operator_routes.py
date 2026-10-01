"""Validate the edge-authenticated operator independently of feature gates."""
from flask import Blueprint, jsonify
from hub.auth import require_operator

bp = Blueprint('operator', __name__)


@bp.get('/api/operator/session')
@require_operator
def session():
    response = jsonify(authenticated=True)
    response.headers['Cache-Control'] = 'no-store'
    return response
