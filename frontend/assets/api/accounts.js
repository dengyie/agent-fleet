import {platformRequest} from './client.js';

export function accountRequest(action, data) {
  return platformRequest('/accounts/' + action, data === undefined ? undefined : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: data,
  });
}
