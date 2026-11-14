import {apiPath} from '../routes.js';
import {platformRequest} from './client.js';

function path(...parts) { return apiPath('platform', 'v1', 'settings', ...parts); }
export function getSettings(owner = '') {
  return platformRequest(path() + (owner ? '?owner_id=' + encodeURIComponent(owner) : ''));
}
export async function saveSetting(kind, id, body, owner = '') {
  const result = await platformRequest(path(kind, id) + (owner ? '?owner_id=' + encodeURIComponent(owner) : ''),
    {method: 'PUT', body, headers: {'Content-Type': 'application/json'}});
  window.dispatchEvent(new Event('fleet-settings-updated'));
  return result;
}
export function getRuntimeSettings(kind = 'runtime') { return platformRequest(path(kind)); }
export function saveRuntimeSettings(kind, body) {
  return platformRequest(path(kind), {method: 'PUT', body, headers: {'Content-Type': 'application/json'}});
}
export async function saveDefaults(defaults, revision, owner = '') {
  if (owner) return saveSetting("defaults", "current", {defaults, revision}, owner);
  const result = await platformRequest(apiPath('platform', 'v1', 'defaults'), {method: 'PUT', body: {defaults, revision}, headers: {'Content-Type': 'application/json'}});
  window.dispatchEvent(new Event('fleet-settings-updated'));
  return result;
}
export function enrollWorkspaceNode(nodeId, body, owner = '') {
  return platformRequest(path('nodes', nodeId, 'enrollment') + (owner ? '?owner_id=' + encodeURIComponent(owner) : ''),
    {method: 'POST', body, headers: {'Content-Type': 'application/json'}});
}
