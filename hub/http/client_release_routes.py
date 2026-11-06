"""hub/http/client_release_routes.py — 节点客户端发布（拉取式更新）薄适配层。

Routes:
  GET /api/client/releases/<channel>/manifest   签名清单 ``{ok, manifest, signature}``
  GET /api/client/releases/artifacts/<sha256>   制品 tar.gz（按内容寻址）

runner 凭据（``X-Runner-Credential``）认证；清单本身另有 Ed25519 签名，节点只信
签名，不信传输。
"""
from flask import Blueprint, Response, current_app, jsonify

from hub.application.errors import ApplicationError
from hub.auth import require_runner
from hub.http.errors import error_response, get_request_id

bp = Blueprint("client_releases", __name__)


def _service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("client_releases")
    if service is None:
        raise ApplicationError("client_release_unavailable", "客户端发布未启用", 503)
    return service


@bp.route("/api/client/releases/<channel>/manifest", methods=["GET"])
@require_runner
def client_manifest(channel):
    try:
        service = _service()
        if channel != service.channel:
            raise ApplicationError("channel_not_found", "未知发布通道", 404)
        signed = service.signed_manifest()
    except ApplicationError as exc:
        return error_response(exc)
    return jsonify({"ok": True, **signed, "request_id": get_request_id()})


@bp.route("/api/client/releases/artifacts/<sha256>", methods=["GET"])
@require_runner
def client_artifact(sha256):
    try:
        data = _service().artifact(sha256)
    except ApplicationError as exc:
        return error_response(exc)
    return Response(data, mimetype="application/gzip", headers={
        "Cache-Control": "no-store",
        "Content-Length": str(len(data)),
    })
