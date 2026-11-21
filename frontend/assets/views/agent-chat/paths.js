import {pagePath} from '../../routes.js';

// Existing document routes keep direct links and refresh working on static and
// Hub hosts. These are browser view modes, not new backend route requirements.
export function nativeChatPath({sessionId, taskId, machine} = {}) {
  if (sessionId) return pagePath('session', sessionId) + '?view=chat';
  if (taskId) return pagePath('task', taskId) + '?view=chat';
  return pagePath('assistant') + '?view=computer' + (machine ? '&machine=' + encodeURIComponent(machine) : '');
}
export function nativeDirectoryPath(machine) {
  return pagePath('assistant') + '?view=sessions' + (machine ? '&machine=' + encodeURIComponent(machine) : '');
}
