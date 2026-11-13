"""Bounded, read-only Streamable HTTP MCP connector.

The connector is deliberately independent of Flask and ToolBroker.  It accepts
one administrator-pinned endpoint and SecretRef, performs one request per
operation, and treats every server supplied description/annotation as
untrusted data.
"""
from __future__ import annotations

import hashlib
import json
import math
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Protocol, Callable
from urllib.parse import SplitResult, urlsplit
from urllib.request import Request

from hub.domain.mcp_catalog import MAX_CATALOG_INTEGER, MCPCatalogError
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import validate_reference
from fleet_core.http_transport import DeadlineResponse, open_http

try:
    from jsonschema import Draft202012Validator, SchemaError
except ImportError:  # pragma: no cover - deployment packaging must include it
    Draft202012Validator = None
    SchemaError = ValueError

PROTOCOL_VERSION = "2026-07-28"
MAX_TOOLS = 100
MAX_EXPOSED_TOOLS = 20
MAX_TOTAL_SCHEMA_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 32 * 1024
MAX_SCHEMA_DEPTH = 16
MAX_RESULT_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


MCPConnectorError = MCPCatalogError


@dataclass(frozen=True, slots=True)
class MCPTransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class MCPTransport(Protocol):
    def request(
        self,
        endpoint: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
    ) -> MCPTransportResponse: ...


def _endpoint(value: object) -> SplitResult:
    if (type(value) is not str or not 1 <= len(value) <= 2048
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value):
        raise MCPConnectorError("invalid_endpoint")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port == 0):
            raise ValueError("invalid HTTPS endpoint")
        parsed.hostname.encode("ascii")
        return parsed
    except (ValueError, UnicodeError) as exc:
        raise MCPConnectorError("invalid_endpoint", cause=exc) from exc


class UrllibMCPTransport:
    """One HTTPS attempt; private aliases pin an exact endpoint to one IP."""

    def __init__(self, *, private_aliases: Mapping[str, str] | None = None):
        self.private_aliases: dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address] = {}
        for endpoint, raw in (private_aliases or {}).items():
            _endpoint(endpoint)
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise MCPConnectorError("invalid_private_alias", cause=exc) from exc
            if (not (address.is_private or address.is_loopback) or address.is_link_local
                    or address.is_multicast or address.is_unspecified or address.is_reserved
                    or getattr(address, "ipv4_mapped", None) is not None):
                raise MCPConnectorError("invalid_private_alias")
            self.private_aliases[endpoint] = address

    def _address_policy(self, endpoint: str) -> Callable[[str, int, tuple[object, ...]], bool]:
        parsed = _endpoint(endpoint)
        private = self.private_aliases.get(endpoint)
        def policy(host: str, family: int, sockaddr: tuple[object, ...]) -> bool:
            try:
                address = ipaddress.ip_address(str(sockaddr[0]))
                if (host.lower() != parsed.hostname.lower() or sockaddr[1] != (parsed.port or 443)
                        or family not in (socket.AF_INET, socket.AF_INET6)
                        or address.is_multicast or address.is_unspecified or address.is_link_local
                        or getattr(address, "ipv4_mapped", None) is not None):
                    return False
                return address == private if private is not None else address.is_global
            except (ValueError, IndexError, TypeError):
                return False
        return policy

    def request(self, endpoint: str, *, headers: Mapping[str, str], body: bytes,
                timeout_s: float) -> MCPTransportResponse:
        policy = self._address_policy(endpoint)
        request = Request(endpoint, data=body, headers=dict(headers), method="POST")
        try:
            with open_http(request, timeout_s=timeout_s, use_proxy=False, address_policy=policy) as response:
                if 300 <= response.status < 400:
                    raise MCPConnectorError("redirect_rejected")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                raw = (_read_sse(response, _decode_message(body).get("id"))
                       if content_type == "text/event-stream" and 200 <= response.status < 300
                       else response.read(MAX_RESULT_BYTES + 1))
                if len(raw) > MAX_RESULT_BYTES:
                    raise MCPConnectorError("result_too_large")
                return MCPTransportResponse(response.status, dict(response.headers.items()), raw)
        except MCPConnectorError:
            raise
        except TimeoutError as exc:
            raise MCPConnectorError("timeout", cause=exc) from exc
        except Exception as exc:
            raise MCPConnectorError("transport_error", cause=exc) from exc


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MCPConnectorError("invalid_json", cause=exc) from exc


def _schema_depth(value: object, depth: int = 0) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise MCPConnectorError("schema_too_deep")
    if isinstance(value, list):
        for item in value:
            _schema_depth(item, depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
            if not isinstance(item, str) or not item.startswith("#"):
                raise MCPConnectorError("external_schema_reference")
        _schema_depth(item, depth + 1)


def _validate_schema(schema: object) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise MCPConnectorError("schema_invalid")
    encoded = _canonical(schema)
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise MCPConnectorError("schema_too_large")
    _schema_depth(schema)
    if Draft202012Validator is None:
        raise MCPConnectorError("schema_validator_unavailable")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise MCPConnectorError("schema_invalid", cause=exc) from exc
    if schema.get("type") not in (None, "object", "array", "string", "number",
                                  "integer", "boolean", "null"):
        raise MCPConnectorError("schema_invalid")
    return schema


def _matches(value: object, schema: Mapping[str, Any]) -> bool:
    if Draft202012Validator is None:
        return False
    try:
        return Draft202012Validator(schema).is_valid(value)
    except Exception:
        return False


def _decode_message(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def reject_constant(value: str) -> None:
        raise ValueError("invalid JSON constant")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)
        _canonical(value)
        if type(value) is not dict:
            raise ValueError("expected JSON object")
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise MCPConnectorError("invalid_response", cause=exc) from exc


class _SSEDecoder:
    """Incremental SSE framing; CRLF and UTF-8 may span arbitrary reads."""

    def __init__(self) -> None:
        self.line = bytearray()
        self.data: list[bytes] = []
        self.after_cr = False

    def feed(self, chunk: bytes) -> Iterator[tuple[int, dict[str, Any]]]:
        for offset, byte in enumerate(chunk, 1):
            if self.after_cr and byte == 10:
                self.after_cr = False
                continue
            self.after_cr = byte == 13
            if byte not in (10, 13):
                self.line.append(byte)
                continue
            line = bytes(self.line)
            self.line.clear()
            if not line and self.data:
                raw = b"\n".join(self.data)
                self.data.clear()
                yield offset, _decode_message(raw)
            elif line == b"data" or line.startswith(b"data:"):
                self.data.append(line[5:].removeprefix(b" "))


def _is_final_response(value: dict[str, Any], expected_id: object) -> bool:
    if value.get("jsonrpc") != "2.0":
        raise MCPConnectorError("invalid_response")
    if "method" in value:
        if "id" in value:
            raise MCPConnectorError("unsupported_callback")
        if type(value["method"]) is not str or not value["method"].startswith("notifications/"):
            raise MCPConnectorError("invalid_response")
        return False
    if type(value.get("id")) is not str or value["id"] != expected_id or ("result" in value) == ("error" in value):
        raise MCPConnectorError("invalid_response")
    return True


def _read_sse(response: DeadlineResponse, expected_id: object) -> bytes:
    decoder, raw = _SSEDecoder(), bytearray()
    while True:
        chunk = response.read1(min(8192, MAX_RESULT_BYTES + 1 - len(raw)))
        if not chunk:
            raise MCPConnectorError("invalid_response")
        base = len(raw)
        raw.extend(chunk)
        if len(raw) > MAX_RESULT_BYTES:
            raise MCPConnectorError("result_too_large")
        for offset, message in decoder.feed(chunk):
            if _is_final_response(message, expected_id):
                return bytes(raw[:base + offset])


def _json_body(response: MCPTransportResponse, expected_id: str) -> dict[str, Any]:
    if type(response.status) is not int or not 200 <= response.status < 300:
        raise MCPConnectorError("http_error")
    if not isinstance(response.body, bytes) or len(response.body) > MAX_RESULT_BYTES:
        raise MCPConnectorError("result_too_large")
    content_type = next((str(value).split(";", 1)[0].strip().lower()
                         for key, value in response.headers.items() if str(key).lower() == "content-type"), "")
    if content_type == "application/json":
        messages = [_decode_message(response.body)]
    elif content_type == "text/event-stream":
        messages = [message for _, message in _SSEDecoder().feed(response.body)]
    else:
        raise MCPConnectorError("invalid_content_type")
    responses = []
    for value in messages:
        if _is_final_response(value, expected_id):
            responses.append(value)
    if len(responses) != 1:
        raise MCPConnectorError("invalid_response")
    if "error" in responses[0]:
        raise MCPConnectorError("remote_error")
    return responses[0]


def _redact(value: object, secret: str) -> object:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {str(key).replace(secret, "[REDACTED]"): _redact(item, secret) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class MCPConnectorConfig:
    connector_id: str
    endpoint: str
    secret_ref: str
    owner_id: str
    workspace_id: str
    selected_tools: tuple[str, ...]
    risk: str
    retry_class: str
    timeout_s: float
    result_limit_bytes: int
    max_tools: int = MAX_TOOLS

    def __post_init__(self) -> None:
        if not isinstance(self.connector_id, str) or not _IDENTIFIER.fullmatch(self.connector_id):
            raise MCPConnectorError("invalid_connector_id")
        try:
            validate_reference(self.owner_id)
            validate_reference(self.workspace_id)
        except WorkflowValidationError as exc:
            raise MCPConnectorError("invalid_connector_scope", cause=exc) from exc
        if not isinstance(self.secret_ref, str) or not self.secret_ref or len(self.secret_ref) > 256:
            raise MCPConnectorError("invalid_secret_ref")
        _endpoint(self.endpoint)
        if not isinstance(self.selected_tools, tuple) or not 1 <= len(self.selected_tools) <= MAX_EXPOSED_TOOLS:
            raise MCPConnectorError("invalid_tool_selection")
        if any(type(value) is not str or not _IDENTIFIER.fullmatch(value) for value in self.selected_tools):
            raise MCPConnectorError("invalid_tool_selection")
        if len(set(self.selected_tools)) != len(self.selected_tools):
            raise MCPConnectorError("invalid_tool_selection")
        if self.risk != "read_only":
            raise MCPConnectorError("unsupported_risk")
        if self.retry_class != "read_only":
            raise MCPConnectorError("unsupported_retry_class")
        if (not isinstance(self.timeout_s, (int, float)) or isinstance(self.timeout_s, bool)
                or not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 30):
            raise MCPConnectorError("invalid_timeout")
        if type(self.result_limit_bytes) is not int or not 1 <= self.result_limit_bytes <= MAX_RESULT_BYTES:
            raise MCPConnectorError("invalid_result_limit")
        if type(self.max_tools) is not int or not 1 <= self.max_tools <= MAX_TOOLS:
            raise MCPConnectorError("invalid_tool_limit")


@dataclass(frozen=True, slots=True)
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    schema_digest: str
    output_schema_digest: str | None
    server_annotations: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MCPDiscovery:
    protocol_version: str
    server_name: str
    server_version: str
    capabilities: dict[str, Any]
    tools: tuple[MCPTool, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    state: str
    structured_content: object | None = None
    content: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None


class MCPConnector:
    def __init__(self, config: MCPConnectorConfig, *, transport: MCPTransport | None = None,
                 secret_broker: Any):
        self.config = config
        self.transport = transport or UrllibMCPTransport()
        self.secret_broker = secret_broker
        self.discovery: MCPDiscovery | None = None
        self.revision = 0

    def _resolve_secret(self) -> str:
        try:
            secret = self.secret_broker.resolve(self.config.secret_ref)
        except Exception as exc:
            raise MCPConnectorError("secret_unavailable", cause=exc) from exc
        if (not isinstance(secret, str) or not 1 <= len(secret) <= 512
                or any(ord(char) < 33 or ord(char) > 126 for char in secret)):
            raise MCPConnectorError("secret_unavailable")
        return secret

    def _request(self, request_id: str, method: str, params: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        secret = self._resolve_secret()
        body = _canonical({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": dict(params)})
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
            "Authorization": "Bearer " + secret,
        }
        if method == "tools/call":
            name = params.get("name")
            if isinstance(name, str):
                headers["Mcp-Name"] = name
        try:
            response = self.transport.request(self.config.endpoint, headers=headers,
                                              body=body, timeout_s=float(self.config.timeout_s))
        except MCPConnectorError:
            raise
        except TimeoutError as exc:
            raise MCPConnectorError("timeout", cause=exc) from exc
        except Exception as exc:
            raise MCPConnectorError("transport_error", cause=exc) from exc
        return _json_body(response, request_id), secret

    def discover(self) -> MCPDiscovery:
        discover, discovery_secret = self._request("discover", "server/discover", {"_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "agent-fleet", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }})
        if discovery_secret in _canonical(discover).decode("utf-8"):
            raise MCPConnectorError("secret_reflection")
        result = discover["result"]
        if (not isinstance(result, dict) or type(result.get("supportedVersions")) is not list
                or PROTOCOL_VERSION not in result["supportedVersions"]):
            raise MCPConnectorError("unsupported_protocol_version")
        metadata = result.get("_meta", {})
        info = metadata.get("io.modelcontextprotocol/serverInfo", {}) if type(metadata) is dict else None
        if not isinstance(info, dict) or not isinstance(result.get("capabilities", {}), dict):
            raise MCPConnectorError("invalid_server_info")
        listed_payload, list_secret = self._request("list", "tools/list", {"_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        }})
        if list_secret in _canonical(listed_payload).decode("utf-8"):
            raise MCPConnectorError("secret_reflection")
        listed = listed_payload["result"]
        if not isinstance(listed, dict) or not isinstance(listed.get("tools"), list):
            raise MCPConnectorError("invalid_tool_list")
        raw_tools = listed["tools"]
        if len(raw_tools) > self.config.max_tools:
            raise MCPConnectorError("too_many_tools")
        selected = set(self.config.selected_tools)
        tools: list[MCPTool] = []
        total_schema = 0
        seen: set[str] = set()
        for raw in raw_tools:
            if (not isinstance(raw, dict) or not isinstance(raw.get("name"), str)
                    or not _IDENTIFIER.fullmatch(raw["name"])):
                raise MCPConnectorError("invalid_tool")
            if raw["name"] in seen:
                raise MCPConnectorError("duplicate_tool")
            seen.add(raw["name"])
            input_schema = _validate_schema(raw.get("inputSchema"))
            output_schema = raw.get("outputSchema")
            if output_schema is not None:
                output_schema = _validate_schema(output_schema)
            input_bytes = _canonical(input_schema)
            output_bytes = _canonical(output_schema) if output_schema is not None else b""
            total_schema += len(input_bytes) + len(output_bytes)
            if total_schema > MAX_TOTAL_SCHEMA_BYTES:
                raise MCPConnectorError("schema_total_too_large")
            if raw["name"] not in selected:
                continue
            tools.append(MCPTool(
                raw["name"], str(raw.get("description") or "")[:8192], input_schema, output_schema,
                hashlib.sha256(input_bytes).hexdigest(),
                hashlib.sha256(output_bytes).hexdigest() if output_schema is not None else None,
                dict(raw.get("annotations") or {}) if isinstance(raw.get("annotations"), dict) else {},
            ))
        if {tool.name for tool in tools} != selected:
            raise MCPConnectorError("selected_tool_missing")
        from .snapshots import discovery_digest

        discovery_value = MCPDiscovery(PROTOCOL_VERSION, str(info.get("name") or "")[:256],
                                       str(info.get("version") or "")[:128],
                                       dict(result.get("capabilities") or {}),
                                       tuple(sorted(tools, key=lambda tool: tool.name)), self.revision or 1)
        if self.discovery is None:
            self.revision = 1
        elif discovery_digest(discovery_value) != discovery_digest(self.discovery):
            if self.revision >= MAX_CATALOG_INTEGER:
                raise MCPConnectorError("connector_revision_exhausted")
            self.revision += 1
        discovery_value = MCPDiscovery(discovery_value.protocol_version, discovery_value.server_name,
            discovery_value.server_version, discovery_value.capabilities, discovery_value.tools, self.revision)
        self.discovery = discovery_value
        return discovery_value

    def call(self, tool_name: str, arguments: Mapping[str, Any]) -> MCPCallResult:
        if self.discovery is None:
            raise MCPConnectorError("discovery_required")
        tool = next((item for item in self.discovery.tools if item.name == tool_name), None)
        if tool is None:
            raise MCPConnectorError("tool_not_selected")
        if (hashlib.sha256(_canonical(tool.input_schema)).hexdigest() != tool.schema_digest
                or (hashlib.sha256(_canonical(tool.output_schema)).hexdigest() if tool.output_schema is not None else None)
                != tool.output_schema_digest):
            raise MCPConnectorError("schema_pin_changed")
        if not isinstance(arguments, Mapping) or not _matches(dict(arguments), tool.input_schema):
            raise MCPConnectorError("input_schema_invalid")
        try:
            payload, secret = self._request("call", "tools/call", {
                "_meta": {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                          "io.modelcontextprotocol/clientCapabilities": {}},
                "name": tool_name, "arguments": dict(arguments),
            })
        except MCPConnectorError as exc:
            if exc.code in {"timeout", "transport_error"}:
                return MCPCallResult("unknown", error_code=exc.code)
            raise
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MCPConnectorError("invalid_call_result")
        if result.get("resultType") == "input_required" or result.get("inputRequests"):
            return MCPCallResult("unknown", error_code="elicitation_pending")
        structured = result.get("structuredContent")
        if tool.output_schema is not None and (structured is None or not _matches(structured, tool.output_schema)):
            raise MCPConnectorError("output_schema_invalid")
        content = result.get("content") or []
        if not isinstance(content, list):
            raise MCPConnectorError("invalid_content")
        if len(content) > 64 or any(type(item) is not dict or type(item.get("type")) is not str for item in content):
            raise MCPConnectorError("invalid_content")
        if len(_canonical({"content": content, "structuredContent": structured})) > self.config.result_limit_bytes:
            raise MCPConnectorError("result_too_large")
        bounded = _redact(content, secret)
        return MCPCallResult("failed" if result.get("isError") else "succeeded",
                             structured_content=_redact(structured, secret),
                             content=tuple(item for item in bounded if isinstance(item, dict)),
                             error_code="remote_tool_error" if result.get("isError") else None)


__all__ = [
    "MCPCallResult", "MCPConnector", "MCPConnectorConfig", "MCPConnectorError",
    "MCPDiscovery", "MCPTool", "MCPTransport", "MCPTransportResponse", "PROTOCOL_VERSION",
]
