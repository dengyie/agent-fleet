import {accountRequest} from '../api/accounts.js';
import {mountAccountLogin} from './account-login.js';
import {getOperatorSession, setOperatorToken} from '../api/client.js';
import {pagePath, uiIcon} from '../routes.js';

export function safeReturnPath(value) {
  if (typeof value !== 'string' || value.length > 2048) return pagePath('assistant');
  try {
    const url = new URL(value, window.location.origin);
    const known = /^\/(?:index\.html|account|assistant|monitoring|(?:machine|task|session|conversation)\/[^/]+)?$/;
    if (url.origin === window.location.origin && known.test(url.pathname)) {
      return url.pathname + url.search + url.hash;
    }
  } catch (_) {}
  return pagePath('assistant');
}

export function redirectToLogin() {
  setOperatorToken('');
  const returnTo = safeReturnPath(window.location.pathname + window.location.search + window.location.hash);
  window.location.replace(pagePath('login') + '?' + new URLSearchParams({return_to: returnTo}));
}

export async function mountLogin() {
  const gate = document.getElementById('access-state');
  gate.hidden = false;
  gate.querySelector('p').textContent = '正在加载登录服务…';
  gate.querySelector('button').hidden = true;
  try {
    const options = await accountRequest("options");
    const returnTo = safeReturnPath(new URLSearchParams(window.location.search).get("return_to"));
    if (options.enabled) { mountAccountLogin(options, returnTo); return; }
  } catch (error) {
    if (error.status !== 404) {
      const gate = document.getElementById("access-state");
      gate.querySelector("p").textContent = "暂时无法加载登录服务";
      const retry = gate.querySelector("button"); retry.hidden = false; retry.onclick = mountLogin;
      return;
    }
  }
  mountTokenLogin();
}

function mountTokenLogin() {
  const host = document.getElementById('login-view');
  const form = document.getElementById('login-form');
  const input = form.querySelector('input');
  const status = document.getElementById('login-status');
  const submit = form.querySelector('[type=submit]');
  const returnTo = safeReturnPath(new URLSearchParams(window.location.search).get('return_to'));
  document.getElementById('access-state').hidden = true;
  document.title = '登录 · Agent Fleet';
  host.hidden = false;
  host.querySelectorAll('[data-login-icon]').forEach(slot => slot.replaceChildren(uiIcon(slot.dataset.loginIcon, {size: 18})));
  let pending = true;
  submit.disabled = true;
  function failure(error) {
    if (error.status === 401 || error.status === 403) {
      setOperatorToken('');
      status.textContent = '令牌无效或登录已失效，请重新输入。';
    } else {
      status.textContent = '暂时无法验证登录状态，请稍后重试。';
      if (error.requestId) status.textContent += ' 请求编号 ' + error.requestId;
    }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending) return;
    pending = true; submit.disabled = true; status.textContent = '正在登录…';
    setOperatorToken(input.value.trim());
    try {
      await getOperatorSession();
      window.location.replace(returnTo);
    } catch (error) { failure(error); }
    finally { pending = false; submit.disabled = false; }
  });
  // An existing edge-authenticated session can enter directly; a token's
  // presence in storage never grants access by itself.
  getOperatorSession().then(() => window.location.replace(returnTo)).catch(error => {
    if (error.status === 401) setOperatorToken('');
    else failure(error);
  }).finally(() => { pending = false; submit.disabled = false; });
}
