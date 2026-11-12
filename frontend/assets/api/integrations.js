/* Strict, secret-free MCP management DTOs; transport stays in client.js. */
import {apiPath} from '../routes.js';
import {platformRequest} from './client.js';
import {ContractError} from './contracts.js';

const summaryFields = ['connector_id', 'owner_id', 'workspace_id', 'endpoint_origin', 'selected_tools',
  'risk', 'retry_class', 'timeout_s', 'result_limit_bytes', 'epoch', 'active_revision', 'pending_revision',
  'state', 'adapter_attached', 'runtime_ready', 'created_at', 'updated_at', 'revoked_at'];
const discoveryFields = ['revision', 'protocol_version', 'server_name', 'server_version', 'capabilities', 'tools', 'content_digest'];
const toolFields = ['name', 'description', 'input_schema', 'output_schema', 'schema_digest', 'output_schema_digest', 'server_annotations'];
const states = new Set(['registered', 'active', 'pending', 'revoked', 'restore_disabled']);
const encoder = new TextEncoder();
function requireValue(condition) {
  if (!condition) throw new ContractError('invalid_mcp_management', 'MCP 目录响应格式无效，请重新读取');
}
function object(value) { requireValue(value !== null && typeof value === 'object' && !Array.isArray(value)); return value; }
function exact(value, fields) {
  object(value);
  requireValue(Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key)));
}
function text(value, maximum, allowEmpty = false) {
  requireValue(typeof value === 'string' && value.length <= maximum && (allowEmpty || value.length > 0));
}
function identifier(value) { text(value, 128); requireValue(/^[A-Za-z0-9_.:-]+$/.test(value)); }
function reference(value) { text(value, 128); requireValue(!value.includes('\0') && encoder.encode(value).length <= 128); }
function integer(value) { requireValue(Number.isSafeInteger(value) && value >= 1); }
function digest(value) { requireValue(typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)); }
function utc(value) {
  requireValue(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value));
  const date = new Date(value); requireValue(Number.isFinite(date.getTime()) && date.toISOString() === value);
}
function boundedJSON(value, limit) {
  try { requireValue(encoder.encode(JSON.stringify(value)).length <= limit); }
  catch (error) { if (error instanceof ContractError) throw error; throw new ContractError('invalid_mcp_management', 'MCP 目录内容无效'); }
}
function summary(value) {
  exact(value, summaryFields);
  identifier(value.connector_id); reference(value.owner_id); reference(value.workspace_id);
  text(value.endpoint_origin, 2048);
  let origin;
  try { origin = new URL(value.endpoint_origin); } catch (error) { throw new ContractError('invalid_mcp_management', 'MCP 地址无效'); }
  requireValue(origin.protocol === 'https:' && !origin.username && !origin.password && origin.pathname === '/' && !origin.search && !origin.hash);
  requireValue(/^https:\/\/[^/?#]+$/.test(value.endpoint_origin));
  requireValue(Array.isArray(value.selected_tools) && value.selected_tools.length >= 1 && value.selected_tools.length <= 20);
  value.selected_tools.forEach(identifier); requireValue(new Set(value.selected_tools).size === value.selected_tools.length);
  requireValue(value.risk === 'read_only' && value.retry_class === 'read_only');
  requireValue(typeof value.timeout_s === 'number' && Number.isFinite(value.timeout_s) && value.timeout_s > 0 && value.timeout_s <= 30);
  integer(value.result_limit_bytes); requireValue(value.result_limit_bytes <= 262144); integer(value.epoch);
  if (value.active_revision !== null) integer(value.active_revision);
  if (value.pending_revision !== null) {
    integer(value.pending_revision); requireValue(value.active_revision !== null && value.pending_revision > value.active_revision);
  }
  requireValue(states.has(value.state) && typeof value.adapter_attached === 'boolean' && typeof value.runtime_ready === 'boolean');
  requireValue(value.runtime_ready === (value.adapter_attached && value.active_revision !== null && !['revoked', 'restore_disabled'].includes(value.state)));
  if (value.state === 'registered') requireValue(value.active_revision === null && value.pending_revision === null);
  if (value.state === 'active') requireValue(value.active_revision !== null && value.pending_revision === null);
  if (value.state === 'pending') requireValue(value.pending_revision !== null);
  utc(value.created_at); utc(value.updated_at);
  requireValue(value.updated_at >= value.created_at);
  if (value.revoked_at !== null) {
    utc(value.revoked_at); requireValue(value.revoked_at >= value.created_at && value.revoked_at <= value.updated_at);
    requireValue(['revoked', 'restore_disabled'].includes(value.state));
  }
  if (value.state === 'revoked') requireValue(value.revoked_at !== null);
  return value;
}
function discovery(value, revision, selected) {
  if (revision === null) { requireValue(value === null); return; }
  exact(value, discoveryFields); integer(value.revision); requireValue(value.revision === revision);
  requireValue(value.protocol_version === '2026-07-28'); text(value.server_name, 256, true); text(value.server_version, 128, true);
  object(value.capabilities); digest(value.content_digest);
  requireValue(Array.isArray(value.tools) && value.tools.length === selected.length);
  const names = new Set(selected); let schemaBytes = 0;
  for (const tool of value.tools) {
    exact(tool, toolFields); identifier(tool.name); requireValue(names.delete(tool.name)); text(tool.description, 8192, true);
    object(tool.input_schema); digest(tool.schema_digest); object(tool.server_annotations);
    boundedJSON(tool.input_schema, 32768); schemaBytes += encoder.encode(JSON.stringify(tool.input_schema)).length;
    if (tool.output_schema === null) requireValue(tool.output_schema_digest === null);
    else {
      object(tool.output_schema); digest(tool.output_schema_digest); boundedJSON(tool.output_schema, 32768);
      schemaBytes += encoder.encode(JSON.stringify(tool.output_schema)).length;
    }
  }
  requireValue(schemaBytes <= 262144); boundedJSON(value, 524800);
}
export function parseMCPPage(value) {
  exact(value, ['ok', 'connectors', 'next_cursor']); requireValue(value.ok === true);
  requireValue(Array.isArray(value.connectors) && value.connectors.length <= 8);
  let previous = null;
  for (const item of value.connectors) {
    summary(item); requireValue(previous === null || item.connector_id > previous); previous = item.connector_id;
  }
  if (value.next_cursor !== null) { identifier(value.next_cursor); requireValue(value.next_cursor === previous); }
  return value;
}
export function parseMCPDetail(value) {
  exact(value, ['ok', 'connector']); requireValue(value.ok === true);
  exact(value.connector, ['summary', 'active', 'pending']); const item = summary(value.connector.summary);
  discovery(value.connector.active, item.active_revision, item.selected_tools);
  discovery(value.connector.pending, item.pending_revision, item.selected_tools);
  return value;
}
function recordPath(action, connectorId) {
  identifier(connectorId);
  return apiPath('platform', 'v1', 'integrations', 'mcp', action) + '?connector_id=' + encodeURIComponent(connectorId);
}
export async function listMCPConnectors(after = null, options) {
  const query = after === null ? '' : '?after=' + encodeURIComponent(after);
  return parseMCPPage(await platformRequest(apiPath('platform', 'v1', 'integrations', 'mcp') + query, options));
}
export async function getMCPConnector(connectorId, options) {
  return parseMCPDetail(await platformRequest(recordPath('record', connectorId), options));
}
export async function manageMCPConnector(connectorId, action, payload, options = {}) {
  requireValue(action === 'admit' || action === 'revoke');
  exact(payload, action === 'admit' ? ['expected_epoch', 'pending_revision'] : ['expected_epoch']);
  integer(payload.expected_epoch); if (action === 'admit') integer(payload.pending_revision);
  return parseMCPDetail(await platformRequest(recordPath(action, connectorId), {...options,
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: payload}));
}

export function parseMCPRegistrationCommand(payload) {
  try {
    exact(payload, ['connector']); object(payload.connector);
    const row = {...payload.connector};
    if (!Object.hasOwn(row, 'max_tools')) row.max_tools = 100;
    exact(row, ['connector_id', 'owner_id', 'workspace_id', 'endpoint', 'secret_ref', 'selected_tools',
      'risk', 'retry_class', 'timeout_s', 'result_limit_bytes', 'max_tools']);
    identifier(row.connector_id); reference(row.owner_id); reference(row.workspace_id);
    text(row.endpoint, 2048); requireValue(!/[\x00-\x20\x7f-\uffff\\]/.test(row.endpoint));
    const endpoint = new URL(row.endpoint);
    requireValue(endpoint.protocol === 'https:' && endpoint.hostname && !endpoint.username && !endpoint.password && !endpoint.hash && endpoint.port !== '0');
    requireValue(typeof row.secret_ref === 'string' && /^env:\/\/[A-Z0-9_]{1,128}$/.test(row.secret_ref));
    requireValue(Array.isArray(row.selected_tools) && row.selected_tools.length >= 1 && row.selected_tools.length <= 20);
    row.selected_tools.forEach(identifier); requireValue(new Set(row.selected_tools).size === row.selected_tools.length);
    requireValue(row.risk === 'read_only' && row.retry_class === 'read_only');
    requireValue(typeof row.timeout_s === 'number' && Number.isFinite(row.timeout_s) && row.timeout_s > 0 && row.timeout_s <= 30);
    integer(row.result_limit_bytes); requireValue(row.result_limit_bytes <= 262144);
    integer(row.max_tools); requireValue(row.max_tools <= 100);
    row.selected_tools = [...row.selected_tools].sort(); boundedJSON(row, 8192);
    return {connector: row};
  } catch (_) {
    throw new ContractError('invalid_mcp_registration', 'MCP 登记配置无效，请核对地址、归属、凭据引用、工具和限额');
  }
}

export async function registerMCPConnector(payload, options = {}) {
  const command = parseMCPRegistrationCommand(payload);
  const result = parseMCPDetail(await platformRequest(apiPath('platform', 'v1', 'integrations', 'mcp', 'register'),
    {...options, method: 'POST', headers: {'Content-Type': 'application/json'}, body: command}));
  const row = command.connector, item = result.connector.summary;
  const authority = /^[^:]+:\/\/([^/?#]+)/.exec(row.endpoint);
  requireValue(authority && item.endpoint_origin === 'https://' + authority[1]);
  requireValue(item.connector_id === row.connector_id && item.owner_id === row.owner_id && item.workspace_id === row.workspace_id);
  requireValue(item.selected_tools.join('\0') === row.selected_tools.join('\0') && item.risk === row.risk && item.retry_class === row.retry_class);
  requireValue(item.timeout_s === row.timeout_s && item.result_limit_bytes === row.result_limit_bytes);
  return result;
}

export async function discoverMCPConnector(connectorId, expectedEpoch, options = {}) {
  integer(expectedEpoch);
  const result = parseMCPDetail(await platformRequest(recordPath('discover', connectorId),
    {...options, timeoutMs: 60000, method: 'POST', headers: {'Content-Type': 'application/json'},
      body: {expected_epoch: expectedEpoch}}));
  requireValue(result.connector.summary.connector_id === connectorId && result.connector.summary.epoch >= expectedEpoch);
  return result;
}
