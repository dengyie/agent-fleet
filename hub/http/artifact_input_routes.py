"""Machine-authenticated fixed input download; grant IDs are never bearer tokens."""
from __future__ import annotations

from flask import Blueprint, Response, current_app, g, request

from hub.auth import require_platform_node
from hub.domain.artifact_receive import ArtifactReceiveError
from hub.http.errors import ApplicationError, error_response

bp = Blueprint("platform_artifact_inputs", __name__, url_prefix="/api/platform/v1/node/artifact-inputs")


@bp.get("/<grant_id>/content")
@require_platform_node
def content(grant_id: str):
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("artifact_inputs")
    if service is None or not current_app.config.get("PLATFORM_ARTIFACT_TRANSFER_ENABLED"):
        return error_response(ApplicationError("artifact_transfer_disabled", "产物传输未启用", 404))
    command_id = request.headers.get("X-Platform-Command-ID", "")
    worker = request.headers.get("X-Platform-Worker-ID", "")
    if (not worker or len(worker) > 128 or len(command_id) > 128
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in worker + command_id)):
        return error_response(ApplicationError("invalid_transfer_headers", "传输命令头不合法", 400))
    if request.args or request.headers.get("Range"):
        return error_response(ApplicationError("invalid_receive_request", "输入领取不接受 URL 参数或区间", 400))
    context = service.content(grant_id, command_id=command_id, worker=worker,
        owner=g.platform_node_owner, node=g.platform_node_id,
        credential_fingerprint=g.platform_node_credential_fingerprint)
    try:
        received, chunks = context.__enter__()
    except ArtifactReceiveError as exc:
        status = {"receive_not_found": 404, "transfer_concurrency_limit": 429,
            "capability_unavailable": 503, "transfer_slot_unavailable": 503}.get(exc.code, 409)
        return error_response(ApplicationError(exc.code, "输入领取不可用", status))

    def stream():
        try:
            yield from chunks
        finally:
            context.__exit__(None, None, None)

    response = Response(stream(), content_type="application/octet-stream", headers={
        "Content-Length": str(received.size), "Content-Disposition": 'attachment; filename="' + received.filename + '"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    response.call_on_close(lambda: context.__exit__(None, None, None))
    return response
