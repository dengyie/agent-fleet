/* Platform assistant API. Network access stays in api/client.js. */
import { apiPath } from '../routes.js';
import { platformRequest } from './client.js';

function object(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(label + ' response is invalid');
  return value;
}
function segment(value) { return typeof value === 'string' ? value : ''; }
function json(method, body) { return { method: method, headers: { 'Content-Type': 'application/json' }, body: body || {} }; }

export async function getPlatformDefaults() { return object(await platformRequest(apiPath('platform', 'v1', 'defaults')), 'defaults'); }
export async function getPlatformModels() { return object(await platformRequest(apiPath('platform', 'v1', 'models')), 'models'); }
export async function getPlatformWorkspaces() { return object(await platformRequest(apiPath('platform', 'v1', 'workspaces')), 'workspaces'); }
export async function getPlatformNodes() { return object(await platformRequest(apiPath('platform', 'v1', 'nodes')), 'nodes'); }
export async function getPlatformArtifacts(workspaceId, limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(1000, Math.floor(limit))) : 100;
  var path = apiPath('platform', 'v1', 'workspaces', segment(workspaceId), 'artifacts') +
    '?limit=' + encodeURIComponent(String(bounded));
  return object(await platformRequest(path), 'artifacts');
}
export function getPlatformArtifactContentUrl(artifactId, workspaceId) {
  var workspace = typeof workspaceId === 'string' ? workspaceId : '';
  return apiPath('platform', 'v1', 'artifacts', segment(artifactId), 'content') +
    '?workspace_id=' + encodeURIComponent(String(workspace));
}
export async function getPlatformArtifactPreview(artifactId, workspaceId) {
  var workspace = typeof workspaceId === 'string' ? workspaceId : '';
  var path = apiPath('platform', 'v1', 'artifacts', segment(artifactId), 'preview') +
    '?workspace_id=' + encodeURIComponent(String(workspace));
  return object(await platformRequest(path), 'artifact preview');
}
export async function getPlatformServices(limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(100, Math.floor(limit))) : 100;
  return object(await platformRequest(apiPath('platform', 'v1', 'services') + '?limit=' + encodeURIComponent(String(bounded))), 'services');
}
export async function getPlatformService(serviceId) {
  return object(await platformRequest(apiPath('platform', 'v1', 'services', segment(serviceId))), 'service');
}
export async function getPlatformIncidents(limit, state) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(50, Math.floor(limit))) : 50;
  var path = apiPath('platform', 'v1', 'incidents') + '?limit=' + encodeURIComponent(String(bounded));
  if (state === 'open' || state === 'closed') path += '&state=' + encodeURIComponent(state);
  return object(await platformRequest(path), 'incidents');
}
export async function requestPlatformServiceAction(serviceId, action, idempotencyKey) {
  if (action !== 'inspect' && action !== 'restart') {
    throw new Error('unsupported service action');
  }
  var payload = { action: action };
  var key = typeof idempotencyKey === 'string' ? idempotencyKey.slice(0, 200) : '';
  var options = json('POST', payload);
  if (key) {
    payload.idempotency_key = key;
    options.headers['Idempotency-Key'] = key;
  }
  return object(await platformRequest(
    apiPath('platform', 'v1', 'services', segment(serviceId), 'actions'), options
  ), 'service action');
}
export async function getPlatformApproval(grantId) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'approvals', segment(grantId))
  ), 'approval');
}
export async function decidePlatformApproval(grantId, decision) {
  if (decision !== 'approve' && decision !== 'reject') {
    throw new Error('unsupported approval decision');
  }
  return object(await platformRequest(
    apiPath('platform', 'v1', 'approvals', segment(grantId), 'decisions'),
    json('POST', { decision: decision })
  ), 'approval decision');
}
export async function getPlatformMemories(limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(100, Math.floor(limit))) : 50;
  return object(await platformRequest(apiPath('platform', 'v1', 'memory') +
    '?limit=' + encodeURIComponent(String(bounded))), 'memories');
}
export async function searchPlatformMemories(query, limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(100, Math.floor(limit))) : 20;
  var value = typeof query === 'string' ? query : '';
  return object(await platformRequest(apiPath('platform', 'v1', 'memory', 'search') +
    '?q=' + encodeURIComponent(value) + '&limit=' + encodeURIComponent(String(bounded))), 'memory search');
}
export async function createConversation(input) { return object(await platformRequest(apiPath('platform', 'v1', 'conversations'), json('POST', input)), 'conversation'); }
export async function getConversations(limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(100, Math.floor(limit))) : 50;
  return object(await platformRequest(apiPath('platform', 'v1', 'conversations') +
    '?limit=' + encodeURIComponent(String(bounded))), 'conversations');
}
export async function getConversation(conversationId) { return object(await platformRequest(apiPath('platform', 'v1', 'conversations', segment(conversationId))), 'conversation'); }
export async function appendConversationTurn(conversationId, input) { return object(await platformRequest(apiPath('platform', 'v1', 'conversations', segment(conversationId), 'turns'), json('POST', input)), 'turn'); }
export async function getRun(runId) { return object(await platformRequest(apiPath('platform', 'v1', 'runs', segment(runId))), 'run'); }
export async function getRunEvents(runId, after) {
  var path = apiPath('platform', 'v1', 'runs', segment(runId), 'events');
  if (typeof after === 'number' && isFinite(after)) path += '?after=' + encodeURIComponent(String(Math.max(0, after)));
  return object(await platformRequest(path), 'run events');
}
export async function cancelRun(runId) { return object(await platformRequest(apiPath('platform', 'v1', 'runs', segment(runId), 'cancel'), json('POST')), 'cancel run'); }
export async function createPlatformLegacyTask(runId, task) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'runs', segment(runId), 'legacy-task'),
    json('POST', { task: task || {} })), 'legacy task');
}
export async function getPlatformLegacyTask(runId) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'runs', segment(runId), 'legacy-task')),
    'legacy task');
}
export async function getUnknownCommands(limit) {
  var path = apiPath('platform', 'v1', 'commands', 'unknown');
  if (typeof limit === 'number' && isFinite(limit)) path += '?limit=' + encodeURIComponent(String(Math.max(1, Math.min(100, limit))));
  return object(await platformRequest(path), 'unknown commands');
}
export async function getCommand(commandId) { return object(await platformRequest(apiPath('platform', 'v1', 'commands', segment(commandId))), 'command'); }
export async function submitCommandReconcile(commandId, input) { return object(await platformRequest(apiPath('platform', 'v1', 'commands', segment(commandId), 'reconcile'), json('POST', input)), 'command reconcile'); }
export async function requestCommandPostcheck(commandId) {
  return object(await platformRequest(apiPath('platform', 'v1', 'commands', segment(commandId), 'postcheck'), json('POST')), 'command postcheck');
}
export async function getCommandPostcheck(commandId) {
  return object(await platformRequest(apiPath('platform', 'v1', 'commands', segment(commandId), 'postcheck')), 'command postcheck');
}

export async function createExecutionWindow(runId, input) {
  var payload = Object.assign({}, input || {}, { run_id: segment(runId) });
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows'), json('POST', payload)
  ), 'execution window');
}
export async function listExecutionWindows(runId, limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(50, Math.floor(limit))) : 20;
  var path = apiPath('platform', 'v1', 'execution-windows') +
    '?limit=' + encodeURIComponent(String(bounded));
  if (typeof runId === 'string' && runId) {
    path += '&run_id=' + encodeURIComponent(runId);
  }
  return object(await platformRequest(path), 'execution windows');
}
export async function getExecutionWindow(windowId) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId))
  ), 'execution window');
}
export async function attachExecutionWindow(windowId, ticket) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'attach'),
    json('POST', { ticket: ticket })
  ), 'execution window attach');
}
export async function reconnectExecutionWindow(windowId, input) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'reconnect'),
    json('POST', input || {})
  ), 'execution window reconnect');
}
export async function acquireExecutionWindowWriter(windowId, input) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'writer'),
    json('POST', input || {})
  ), 'execution window writer');
}
export async function renewExecutionWindowWriter(windowId, input) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'writer', 'renew'),
    json('POST', input || {})
  ), 'execution window writer renew');
}
export async function releaseExecutionWindowWriter(windowId, input) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'writer', 'release'),
    json('POST', input || {})
  ), 'execution window writer release');
}
export async function closeExecutionWindow(windowId) {
  return object(await platformRequest(
    apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'close'),
    json('POST')
  ), 'execution window close');
}
export async function getExecutionWindowEvents(windowId, after, limit) {
  var bounded = typeof limit === 'number' && isFinite(limit)
    ? Math.max(1, Math.min(200, Math.floor(limit))) : 100;
  var cursor = typeof after === 'number' && isFinite(after)
    ? Math.max(0, Math.floor(after)) : 0;
  var path = apiPath('platform', 'v1', 'execution-windows', segment(windowId), 'events') +
    '?after=' + encodeURIComponent(String(cursor)) +
    '&limit=' + encodeURIComponent(String(bounded));
  return object(await platformRequest(path), 'execution window events');
}
export function getExecutionWindowFrameUrl(windowId, artifactId, cacheKey) {
  var path = apiPath('platform', 'v1', 'execution-windows', segment(windowId),
    'frames', segment(artifactId));
  return cacheKey ? path + '?retry=' + encodeURIComponent(String(cacheKey)) : path;
}
