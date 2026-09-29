"""Owner-scoped operator endpoints for workspace artifacts."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request
from platform_schema import validate_id

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response
from tools.platform.artifacts import ArtifactError

bp = Blueprint("platform_artifacts", __name__, url_prefix="/api/platform/v1")


def _store():
    store = current_app.extensions.get("fleet", {}).get("services", {}).get("platform_artifacts")
    if store is None:
        raise ApplicationError("artifact_store", "文件存储不可用", 503)
    return store


def _scope(workspace_id: str) -> str:
    try:
        return validate_id(workspace_id, "workspace_id")
    except ValueError:
        raise ApplicationError("invalid_workspace", "workspace_id 不合法", 400)


def _call(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except (ArtifactError, ValueError, TypeError) as exc:
        if not isinstance(exc, ArtifactError):
            return error_response(ApplicationError("invalid_request", "请求参数不合法", 400))
        status = {"not_found": 404, "artifact_corrupt": 409, "invalid_scope": 400, "invalid_artifact_id": 400, "invalid_content_type": 400, "invalid_artifact_name": 400, "artifact_too_large": 413, "preview_unsupported": 415}.get(exc.code, 503)
        detail = {"not_found": "文件不存在", "artifact_corrupt": "文件校验失败", "invalid_scope": "作用域不合法", "invalid_artifact_id": "artifact_id 不合法", "invalid_content_type": "文件类型不合法", "invalid_artifact_name": "文件名不合法", "artifact_too_large": "文件超出大小限制", "preview_unsupported": "该文件类型不支持预览"}.get(exc.code, "文件存储不可用")
        return error_response(ApplicationError(exc.code, detail, status))


@bp.get("/workspaces/<workspace_id>/artifacts")
@require_operator
def list_artifacts(workspace_id):
    return _call(lambda: jsonify({"ok": True, "artifacts": _store().list(g.operator, _scope(workspace_id), limit=request.args.get("limit", 100))}))


@bp.get("/artifacts/<artifact_id>/content")
@require_operator
def artifact_content(artifact_id):
    def _read():
        workspace_id = _scope(request.args.get("workspace_id"))
        manifest = _store().get(g.operator, workspace_id, artifact_id)
        if manifest is None:
            raise ArtifactError("not_found")
        # Re-read through the integrity-checking API before sending bytes.
        raw = _store().read(g.operator, workspace_id, artifact_id)
        response = current_app.response_class(raw, mimetype=manifest["content_type"])
        filename = "".join(
            char if ord(char) >= 0x20 and ord(char) != 0x7f and char not in {"\\", '"'} else "_"
            for char in str(manifest["name"])
        )
        response.headers["Content-Disposition"] = f"attachment; filename=\"{filename}\""
        response.headers["X-Artifact-Sha256"] = manifest["sha256"]
        return response
    return _call(_read)


@bp.get("/artifacts/<artifact_id>/preview")
@require_operator
def artifact_preview(artifact_id):
    def _preview():
        workspace_id = _scope(request.args.get("workspace_id"))
        store = _store()
        manifest = store.get(g.operator, workspace_id, artifact_id)
        if manifest is None:
            raise ArtifactError("not_found")
        preview = store.read_preview(g.operator, workspace_id, artifact_id)
        return jsonify({"ok": True, "artifact": manifest, "preview": preview})
    return _call(_preview)


__all__ = ["bp"]
