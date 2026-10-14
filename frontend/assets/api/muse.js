import {apiPath} from '../routes.js';
import {platformRequest} from './client.js';

const path = (...parts) => apiPath('platform', 'v1', 'muse', ...parts);
const query = values => Object.entries(values || {}).filter(([, value]) => value !== undefined && value !== null && value !== '')
  .map(([key, value]) => encodeURIComponent(key) + '=' + encodeURIComponent(value)).join('&');
const read = (...parts) => platformRequest(path(...parts), {timeoutMs: 60000});
const write = (parts, body, method = 'POST') => platformRequest(path(...parts), {
  method, headers: {'Content-Type': 'application/json'}, body, timeoutMs: 60000,
});
export const museStatus = accountId => platformRequest(path('status') + '?' + query({account_id: accountId}), {timeoutMs: 60000});
export const museImport = (login, accountId, label) => write(['login'], {login, ...(accountId ? {account_id: accountId} : {}), ...(label ? {label} : {})});
export const museDisconnect = (accountId, revision) => write(['disconnect'], {account_id: accountId, connection_revision: revision});
export const museSessions = (accountId, revision) => platformRequest(path('sessions') + '?' + query({account_id: accountId, connection_revision: revision}), {timeoutMs: 60000});
export const museHistory = (id, accountId, revision, before, after, continuation) => platformRequest(path('sessions', id, 'history') + '?' + query({account_id: accountId, connection_revision: revision, before, after, continuation}), {timeoutMs: 60000});
export const museSend = (id, text, requestId, replyTo, accountId, revision) => write(['sessions', id, 'messages'], {
  text, request_id: requestId, account_id: accountId, connection_revision: revision, ...(replyTo ? {reply_to: replyTo} : {}),
});
export const museCancel = (id, accountId, revision) => write(['sessions', id, 'cancel'], {request_id: crypto.randomUUID(), account_id: accountId, connection_revision: revision});
export const museApprovals = (accountId, revision) => platformRequest(path('approvals') + '?' + query({account_id: accountId, connection_revision: revision}), {timeoutMs: 60000});
export const museResource = (file, accountId, revision) => platformRequest(path('resource') + '?' + query({path: file, account_id: accountId, connection_revision: revision}), {timeoutMs: 60000});
export const museDecide = (id, decision, messageRevision, accountId, revision) => write(['approvals', id, 'decision'], {decision, revision: messageRevision, account_id: accountId, connection_revision: revision});
export const museSettings = (accountId, connectionRevision, settingsRevision, sessionId, enabled, autoApproveAll) => write(['settings'], {
  account_id: accountId, connection_revision: connectionRevision, settings_revision: settingsRevision,
  ...(sessionId ? {session_id: sessionId, enabled} : {}),
  ...(typeof autoApproveAll === 'boolean' ? {auto_approve_all: autoApproveAll} : {}),
}, 'PUT');
