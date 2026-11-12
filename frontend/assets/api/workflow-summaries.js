import {apiPath} from '../routes.js';
import {platformRequest} from './client.js';
import {ContractError} from './contracts.js';
export {ContractError} from './contracts.js';

const fields = {
  head: ['summary_id', 'workspace_id', 'latest_revision', 'epoch', 'enabled'],
  version: ['owner_id', 'workspace_id', 'summary_id', 'revision', 'title', 'content', 'created_by', 'created_at_ms', 'source_kind', 'schema_version', 'sha256'],
};

function invalid() { throw new ContractError('invalid_workflow_summary', '工作流摘要响应无效，请重新读取'); }
function exact(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== keys.length || keys.some(key => !Object.hasOwn(value, key))) invalid();
}
function reference(value) {
  if (typeof value !== 'string' || !value.length || value.length > 128 || value.includes('\0') || new TextEncoder().encode(value).length > 128) invalid();
}
function integer(value, minimum) { if (!Number.isSafeInteger(value) || value < minimum) invalid(); }
function digest(value) { if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) invalid(); }
function parseProvenance(value) {
  exact(value, ['run_id', 'source_run_ids', 'source_sha256', 'model_profile_id', 'provider_digest']);
  reference(value.run_id); reference(value.model_profile_id); digest(value.source_sha256); digest(value.provider_digest);
  parseSources(value.source_run_ids);
  return value;
}
function parseSources(values) {
  if (!Array.isArray(values) || values.length < 1 || values.length > 16 || new Set(values).size !== values.length) invalid();
  values.forEach(reference);
}
function parseHead(value) {
  exact(value, fields.head);
  reference(value.summary_id); reference(value.workspace_id);
  integer(value.latest_revision, 1); integer(value.epoch, value.latest_revision);
  if (typeof value.enabled !== 'boolean') invalid();
  return value;
}
function parseVersion(value) {
  const generated = value?.schema_version === 2;
  exact(value, generated ? [...fields.version, 'generation'] : fields.version);
  reference(value.owner_id); reference(value.workspace_id); reference(value.summary_id); reference(value.created_by);
  integer(value.revision, 1); integer(value.created_at_ms, 0);
  if (typeof value.title !== 'string' || !value.title.trim() || value.title !== value.title.trim() || new TextEncoder().encode(value.title).length > 160 ||
      typeof value.content !== 'string' || !value.content.trim() || new TextEncoder().encode(value.content).length > 8192 ||
      value.created_by !== value.owner_id || value.source_kind !== (generated ? 'model' : 'operator') ||
      value.schema_version !== (generated ? 2 : 1)) invalid();
  if (generated) parseProvenance(value.generation);
  digest(value.sha256);
  return value;
}
function parseEnvelope(value, key) {
  exact(value, ['ok', key]); if (value.ok !== true) invalid();
  return value[key];
}
export function parseWorkflowSummaryHeadResponse(value) { return parseHead(parseEnvelope(value, 'summary')); }
export function parseWorkflowSummaryVersionResponse(value) { return parseVersion(parseEnvelope(value, 'summary')); }
export function parseWorkflowSummaryPage(value) {
  exact(value, ['ok', 'summaries', 'next_cursor']);
  if (value.ok !== true || !Array.isArray(value.summaries) || value.summaries.length > 100) invalid();
  const summaries = value.summaries.map(parseHead);
  if (value.next_cursor !== null) {
    reference(value.next_cursor);
    if (!summaries.length || summaries.at(-1).summary_id !== value.next_cursor) invalid();
  }
  return {summaries, next_cursor: value.next_cursor};
}
export function parseWorkflowSummaryPublishResponse(value) {
  const summary = parseEnvelope(value, 'summary');
  parseVersion(summary);
  return {ok: true, summary};
}
export function parseWorkflowSummaryGenerationResponse(envelope) {
  const value = parseEnvelope(envelope, 'generation');
  exact(value, ['run_id', 'workspace_id', 'summary_id', 'title', 'expected_epoch', 'state', 'model_profile_id',
    'source_run_ids', 'source_sha256', 'created_at_ms', 'candidate', 'error_code', 'published_revision']);
  for (const key of ['run_id', 'workspace_id', 'summary_id', 'model_profile_id']) reference(value[key]);
  integer(value.expected_epoch, 0); integer(value.created_at_ms, 0); digest(value.source_sha256); parseSources(value.source_run_ids);
  if (typeof value.title !== 'string' || !value.title.trim() || value.title !== value.title.trim() ||
      new TextEncoder().encode(value.title).length > 160 ||
      !['queued', 'running', 'waiting_node', 'cancelling', 'succeeded', 'failed', 'unknown', 'cancelled'].includes(value.state)) invalid();
  if (value.candidate !== null && (typeof value.candidate !== 'string' || !value.candidate.trim() ||
      new TextEncoder().encode(value.candidate).length > 8192 || value.state !== 'succeeded')) invalid();
  if (value.error_code !== null && !['summary_output_too_large', 'summary_output_empty'].includes(value.error_code)) invalid();
  if (value.error_code !== null && (value.candidate !== null || value.state !== 'succeeded')) invalid();
  if (value.state === 'succeeded' && value.candidate === null && value.error_code === null) invalid();
  if (value.published_revision !== null) {
    integer(value.published_revision, 1);
    if (value.state !== 'succeeded' || value.candidate === null) invalid();
  }
  return value;
}
function query(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) params.set(key, String(value));
  return '?' + params.toString();
}
function post(path, body, options = {}) {
  return platformRequest(path, {...options, method: 'POST', headers: {'Content-Type': 'application/json'}, body});
}

export async function listWorkflowSummaries(workspaceId, cursor = null, options) {
  const values = {workspace_id: workspaceId, limit: 50};
  if (cursor) values.cursor = cursor;
  return parseWorkflowSummaryPage(await platformRequest(apiPath('platform', 'v1', 'workflow-summaries') + query(values), options));
}
export async function getWorkflowSummaryVersion(workspaceId, summaryId, revision, options) {
  const value = await platformRequest(apiPath('platform', 'v1', 'workflow-summaries', 'version') + query({
    workspace_id: workspaceId, summary_id: summaryId, revision,
  }), options);
  return {summary: parseWorkflowSummaryVersionResponse(value)};
}
export async function getWorkflowSummaryHead(workspaceId, summaryId, options) {
  const value = await platformRequest(apiPath('platform', 'v1', 'workflow-summaries', 'head') + query({
    workspace_id: workspaceId, summary_id: summaryId,
  }), options);
  return {summary: parseWorkflowSummaryHeadResponse(value)};
}
export async function publishWorkflowSummary(payload, options) {
  return parseWorkflowSummaryPublishResponse(await post(apiPath('platform', 'v1', 'workflow-summaries'), payload, options));
}
export async function setWorkflowSummaryEnabled(payload, options) {
  const value = await post(apiPath('platform', 'v1', 'workflow-summaries', 'enabled'), payload, options);
  return {summary: parseWorkflowSummaryHeadResponse(value)};
}

export async function createWorkflowSummaryGeneration(payload, options) {
  return parseWorkflowSummaryGenerationResponse(await post(apiPath('platform', 'v1', 'workflow-summary-generations'), payload, options));
}
export async function getWorkflowSummaryGeneration(runId, options) {
  return parseWorkflowSummaryGenerationResponse(await platformRequest(apiPath('platform', 'v1', 'workflow-summary-generations', 'run') + query({run_id: runId}), options));
}
export async function findWorkflowSummaryGeneration(clientToken, options) {
  return parseWorkflowSummaryGenerationResponse(await platformRequest(apiPath('platform', 'v1', 'workflow-summary-generations', 'token') + query({client_token: clientToken}), options));
}
export async function publishGeneratedWorkflowSummary(runId, options) {
  return parseWorkflowSummaryPublishResponse(await post(apiPath('platform', 'v1', 'workflow-summary-generations', 'publish'), {run_id: runId}, options));
}
