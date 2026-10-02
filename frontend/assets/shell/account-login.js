import {uiIcon} from '../routes.js';
import {accountRequest} from '../api/accounts.js';
import {getOperatorSession, setOperatorToken} from '../api/client.js';

export function mountAccountLogin(options, returnTo) {
  const host = document.getElementById('login-view');
  const form = document.getElementById('login-form');
  const title = document.getElementById('login-title');
  document.getElementById('access-state').hidden = true;
  host.hidden = false;
  host.querySelectorAll('[data-login-icon]').forEach(slot => slot.replaceChildren(uiIcon(slot.dataset.loginIcon, {size: 18})));
  let mode = 'login';
  let busy = false;
  function render(message = '') {
    form.replaceChildren();
    title.textContent = {login: '登录工作空间', register: '创建账号', reset: '找回密码'}[mode];
    document.title = title.textContent + ' · Agent Fleet';
    const fields = {};
    const field = (key, label, type, autocomplete) => {
      const wrapper = document.createElement('div'); wrapper.className = 'account-field';
      const labelEl = document.createElement('label'); labelEl.textContent = label; labelEl.htmlFor = 'account-' + key;
      const input = document.createElement('input'); input.id = labelEl.htmlFor; input.type = type;
      input.autocomplete = autocomplete; input.required = true;
      wrapper.append(labelEl, input); form.append(wrapper); fields[key] = input;
      return input;
    };
    field('email', mode === 'login' ? '账号或邮箱' : '邮箱',
      mode === 'login' ? 'text' : 'email', mode === 'login' ? 'username' : 'email').maxLength = 254;
    if (mode === 'register') field('name', '昵称', 'text', 'nickname').maxLength = 80;
    if (mode !== 'login') {
      const code = field('code', '邮箱验证码', 'text', 'one-time-code'); code.pattern = '[0-9]{6}'; code.maxLength = 6; code.inputMode = 'numeric';
      const send = document.createElement('button'); send.type = 'button'; send.textContent = '发送验证码';
      send.disabled = !options.mail_available;
      send.addEventListener('click', async () => {
        if (!fields.email.reportValidity() || busy) return;
        busy = true; send.disabled = true;
        try {
          const response = await accountRequest('code', {email: fields.email.value, purpose: mode === 'register' ? 'register' : 'reset'});
          status.textContent = response.detail;
        } catch (error) { status.textContent = error.detail || '验证码发送失败'; }
        finally { busy = false; send.disabled = !options.mail_available; }
      });
      form.append(send);
      if (!options.mail_available) {
        const note = document.createElement('p'); note.textContent = '邮件服务尚未配置，请联系管理员。'; form.append(note);
      } else if (mode === 'register' && options.registration === 'invite') {
        const note = document.createElement('p'); note.textContent = '目前仅接受受邀邮箱注册，请先联系管理员。'; form.append(note);
      }
    }
    const password = field('password', mode === 'reset' ? '新密码' : '密码', 'password', mode === 'login' ? 'current-password' : 'new-password');
    password.maxLength = 128;
    if (mode !== 'login') {
      password.minLength = 12; password.placeholder = '12–128 个字符';
      field('confirmation', '确认密码', 'password', 'new-password').maxLength = 128;
    }
    const status = document.createElement('p'); status.id = 'login-status'; status.setAttribute('role', 'status'); status.textContent = message;
    const submit = document.createElement('button'); submit.type = 'submit'; submit.className = 'button-primary';
    submit.textContent = {login: '登录', register: '验证并注册', reset: '重置密码'}[mode];
    form.append(status, submit);
    const links = document.createElement('div'); links.className = 'account-links';
    for (const [target, label] of Object.entries({login: '返回登录', register: '注册账号', reset: '忘记密码'})) {
      if (target === mode) continue;
      const link = document.createElement('button'); link.type = 'button'; link.textContent = label;
      link.addEventListener('click', () => { if (!busy) { mode = target; render(); } }); links.append(link);
    }
    form.append(links);
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      if (fields.confirmation && fields.confirmation.value !== password.value) { status.textContent = '两次输入的密码不一致'; return; }
      busy = true; submit.disabled = true; status.textContent = '正在处理…';
      try {
        const data = Object.fromEntries(Object.entries(fields).filter(([key]) => key !== 'confirmation').map(([key, input]) => [key, input.value]));
        await accountRequest(mode, data);
        if (mode === 'login') { setOperatorToken(''); window.location.replace(returnTo); }
        else { const message = mode === 'register' ? '注册成功，请登录。' : '密码已重置，请使用新密码登录。'; mode = 'login'; render(message); }
      } catch (error) { status.textContent = error.detail || '暂时无法完成，请稍后重试'; }
      finally { busy = false; submit.disabled = false; }
    };
  }
  render();
  getOperatorSession().then(() => window.location.replace(returnTo)).catch(() => {});
}
