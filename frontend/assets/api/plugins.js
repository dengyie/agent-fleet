/* Plugin HTTP contract; shared client owns network, deadlines and auth. */
import {apiPath} from '../routes.js';
import {platformRequest} from './client.js';

function path(...parts) { return apiPath('platform', 'v1', 'plugins', ...parts); }
function json(method, body) { return {method, body, headers: {'Content-Type': 'application/json'}}; }
export function getPlugins() { return platformRequest(path()); }
export function enrollPluginNode(node_id, label) { return platformRequest(path('nodes'), json('POST', {node_id, label})); }
export function configurePlugin(id, enabled, revision) { return platformRequest(path(id), json('PUT', {enabled, revision})); }
export function getPluginNodes(id) { return platformRequest(path(id, 'nodes')); }
export function configurePluginNode(id, node, enabled) { return platformRequest(path(id, 'nodes', node), json('PUT', {enabled})); }
export function getPluginOperations(id, scope = {}) {
  const query = new URLSearchParams(scope).toString();
  return platformRequest(path(id, 'operations') + (query ? '?' + query : ''));
}
export function submitPluginOperation(id, node, action, args, key) {
  return platformRequest(path(id, 'operations'), json('POST', {node_id: node, action,
    arguments: args, idempotency_key: key}));
}
