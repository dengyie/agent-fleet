/* Secret-free Fleet identity DTOs; all HTTP deadlines stay in client.js. */
import {apiPath} from '../routes.js';
import {platformRequest} from './client.js';
import {ContractError} from './contracts.js';

const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', {ignoreBOM: true});
const summaryFields = ['owner_id', 'integration_id', 'scopes', 'template_count', 'workspace_count',
  'max_artifacts', 'max_budget_tokens', 'max_budget_requests', 'state', 'created_at', 'revoked_at'];
const scopes = new Set(['workflow.create_from_template', 'workflow.get', 'workflow.list_artifacts']);
const states = new Set(['active', 'revoked', 'restore_disabled']);
function check(condition) {
  if (!condition) throw new ContractError('invalid_identity_management', 'Fleet 身份响应格式无效，请重新读取');
}
function exact(value, fields) {
  check(value !== null && typeof value === 'object' && !Array.isArray(value));
  check(Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key)));
}
function reference(value) {
  check(typeof value === 'string' && value.length > 0 && value.length <= 128 && !value.includes('\0'));
  const bytes = encoder.encode(value);
  check(bytes.length <= 128 && decoder.decode(bytes) === value);
}
function integer(value, minimum, maximum) { check(Number.isSafeInteger(value) && value >= minimum && value <= maximum); }
function compare(left, right) {
  const a = encoder.encode(left), b = encoder.encode(right);
  for (let i = 0; i < Math.min(a.length, b.length); i += 1) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}
function key(value) { exact(value, ['owner_id', 'integration_id']); reference(value.owner_id); reference(value.integration_id); }
function compareKey(a, b) { return compare(a.owner_id, b.owner_id) || compare(a.integration_id, b.integration_id); }
function utc(value) {
  check(typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(value));
  const date = new Date(value); check(Number.isFinite(date.getTime()) && date.toISOString() === value);
}
function references(value, count) {
  check(Array.isArray(value) && value.length === count);
  let previous = null;
  for (const item of value) { reference(item); check(previous === null || compare(previous, item) < 0); previous = item; }
}
function summary(value) {
  exact(value, summaryFields); reference(value.owner_id); reference(value.integration_id);
  check(Array.isArray(value.scopes) && value.scopes.length <= 3);
  let previous = null;
  for (const scope of value.scopes) {
    check(scopes.has(scope) && (previous === null || compare(previous, scope) < 0)); previous = scope;
  }
  integer(value.template_count, 1, 128); integer(value.workspace_count, 1, 128);
  integer(value.max_artifacts, 1, 100); integer(value.max_budget_tokens, 0, 2000000); integer(value.max_budget_requests, 0, 1000);
  check(states.has(value.state)); utc(value.created_at);
  if (value.revoked_at !== null) { utc(value.revoked_at); check(value.revoked_at >= value.created_at && value.state !== 'active'); }
  if (value.state === 'revoked') check(value.revoked_at !== null);
  return value;
}
export function parseIdentityPage(value) {
  exact(value, ['ok', 'identities', 'next_cursor']); check(value.ok === true);
  check(Array.isArray(value.identities) && value.identities.length <= 8);
  let previous = null;
  for (const item of value.identities) { summary(item); check(previous === null || compareKey(previous, item) < 0); previous = item; }
  if (value.next_cursor !== null) { key(value.next_cursor); check(previous !== null && compareKey(value.next_cursor, previous) === 0); }
  return value;
}
export function parseIdentityDetail(value) {
  exact(value, ['ok', 'identity']); check(value.ok === true);
  exact(value.identity, ['summary', 'template_ids', 'workspace_ids']); const item = summary(value.identity.summary);
  references(value.identity.template_ids, item.template_count); references(value.identity.workspace_ids, item.workspace_count);
  return value;
}
export function parseIdentityRevocation(value) {
  exact(value, ['ok', 'revocation']); check(value.ok === true);
  exact(value.revocation, ['owner_id', 'integration_id', 'state']); reference(value.revocation.owner_id); reference(value.revocation.integration_id);
  check(value.revocation.state === 'revoked'); return value;
}
function path(action, ownerId, integrationId) {
  reference(ownerId); reference(integrationId);
  return apiPath('platform', 'v1', 'integrations', 'fleet', action) + '?owner_id=' + encodeURIComponent(ownerId) +
    '&integration_id=' + encodeURIComponent(integrationId);
}
export async function listIntegrationIdentities(after = null, options) {
  if (after !== null) key(after);
  const query = after === null ? '' : '?after_owner_id=' + encodeURIComponent(after.owner_id) + '&after_integration_id=' + encodeURIComponent(after.integration_id);
  return parseIdentityPage(await platformRequest(apiPath('platform', 'v1', 'integrations', 'fleet') + query, options));
}
export async function getIntegrationIdentity(ownerId, integrationId, options) {
  const result = parseIdentityDetail(await platformRequest(path('record', ownerId, integrationId), options));
  check(result.identity.summary.owner_id === ownerId && result.identity.summary.integration_id === integrationId); return result;
}
export async function revokeIntegrationIdentity(ownerId, integrationId, options = {}) {
  const result = parseIdentityRevocation(await platformRequest(path('revoke', ownerId, integrationId), {...options,
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: {expected_state: 'active'}}));
  check(result.revocation.owner_id === ownerId && result.revocation.integration_id === integrationId); return result;
}
