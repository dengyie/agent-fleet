// Optional providers belong to the form that needs them, not the app shell.
const sources = {
  hcaptcha: 'https://js.hcaptcha.com/1/api.js?render=explicit',
  turnstile: 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit',
};
const pending = new Map();

function loadProvider(name) {
  if (window[name]?.render) return Promise.resolve(window[name]);
  if (pending.has(name)) return pending.get(name);
  const script = document.createElement('script');
  script.src = sources[name]; script.async = true;
  const promise = new Promise((resolve, reject) => {
    const finish = error => {
      clearTimeout(timer); script.onload = null; script.onerror = null;
      if (error) { script.remove(); reject(error); }
      else resolve(window[name]);
    };
    const timer = setTimeout(() => finish(new Error('captcha_load_timeout')), 10000);
    script.onload = () => finish(window[name]?.render ? null : new Error('captcha_api_missing'));
    script.onerror = () => finish(new Error('captcha_load_failed'));
  });
  pending.set(name, promise);
  promise.then(() => pending.delete(name), () => pending.delete(name));
  document.head.append(script);
  return promise;
}

export function mountCaptcha(name, sitekey, parent) {
  const host = document.createElement('div');
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  const retry = document.createElement('button'); retry.type = 'button';
  retry.textContent = '重新加载人机验证'; retry.hidden = true;
  const box = document.createElement('div');
  host.append(status, box, retry); parent.append(host);
  let active = true, loading = false, api = null, widget = null;
  const start = async () => {
    if (!active || loading) return;
    loading = true; retry.hidden = true; status.textContent = '正在加载人机验证服务…';
    try {
      const loaded = await loadProvider(name);
      if (!active) return;
      api = loaded;
      widget = api.render(box, {sitekey, size: 'normal',
        theme: document.documentElement.getAttribute('data-resolved-theme') || 'light'});
      status.textContent = '';
    } catch (_) {
      if (!active) return;
      box.replaceChildren();
      status.textContent = '人机验证加载失败，请检查网络后重试。'; retry.hidden = false;
    } finally { loading = false; }
  };
  retry.addEventListener('click', start);
  start();
  return {
    getResponse: () => active && widget !== null ? api.getResponse(widget) : '',
    reset: () => { if (active && widget !== null) api.reset(widget); },
    remove: () => {
      active = false;
      if (widget !== null) { api.remove(widget); widget = null; }
      host.remove();
    },
  };
}
