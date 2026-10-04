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
  let codeAttempts = 0;
  let hcaptchaWidgetId = null;
  let turnstileWidgetId = null;

  function render(message = '') {
    form.replaceChildren();
    hcaptchaWidgetId = null;
    turnstileWidgetId = null;
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
    if (mode === 'register') {
      field('name', '昵称', 'text', 'nickname').maxLength = 80;
      if (options.registration === 'invite') {
        const inviteInput = field('invite_code', '邀请码', 'text', 'off');
        inviteInput.maxLength = 64;
        inviteInput.placeholder = '请输入管理员提供的邀请码（如 inv_...）';
        const note = document.createElement('p');
        note.className = 'account-hint';
        note.textContent = '当前工作空间为邀请制，请向管理员索取一次性邀请码。';
        form.append(note);
      }
    }
    if (mode !== 'login') {
      const code = field('code', '邮箱验证码', 'text', 'one-time-code'); code.pattern = '[0-9]{6}'; code.maxLength = 6; code.inputMode = 'numeric';
      
      const captchaWrapper = document.createElement('div');
      captchaWrapper.className = 'account-captcha-container';
      captchaWrapper.style.margin = '10px 0';
      form.append(captchaWrapper);

      const ensureHcaptcha = () => {
        if (!options.hcaptcha_sitekey) return null;
        if (typeof window.hcaptcha === 'undefined') {
          status.textContent = '正在加载人机验证服务…若长时间未显示，请检查网络或关闭广告拦截插件。';
          return null;
        }
        if (hcaptchaWidgetId !== null) return hcaptchaWidgetId;
        const box = document.createElement('div');
        box.id = 'hcaptcha-slot';
        captchaWrapper.append(box);
        try {
          hcaptchaWidgetId = window.hcaptcha.render(box, {
            sitekey: options.hcaptcha_sitekey,
            size: 'normal',
            theme: document.documentElement.getAttribute('data-resolved-theme') || 'light'
          });
          return hcaptchaWidgetId;
        } catch (e) {
          status.textContent = '人机验证组件渲染失败，请刷新页面重试';
          return null;
        }
      };

      const send = document.createElement('button'); send.type = 'button'; send.textContent = '发送验证码';
      send.disabled = !options.mail_available;
      send.addEventListener('click', async () => {
        if (!fields.email.reportValidity() || busy) return;
        if (mode === 'register' && options.registration === 'invite' && fields.invite_code && !fields.invite_code.reportValidity()) return;
        
        // If consecutive attempts reach 3 or more, proactively render hCaptcha
        if (codeAttempts >= 2 && options.hcaptcha_sitekey) {
          ensureHcaptcha();
        }

        busy = true; send.disabled = true;
        try {
          const payload = {email: fields.email.value, purpose: mode === 'register' ? 'register' : 'reset'};
          if (fields.invite_code && fields.invite_code.value.trim()) payload.invite_code = fields.invite_code.value.trim();
          
          if (hcaptchaWidgetId !== null && typeof window.hcaptcha !== 'undefined') {
            const hresp = window.hcaptcha.getResponse(hcaptchaWidgetId);
            if (hresp) payload.hcaptcha_response = hresp;
          }
          if (options.turnstile_sitekey && typeof window.turnstile !== 'undefined' && turnstileWidgetId !== null) {
            const tresp = window.turnstile.getResponse(turnstileWidgetId);
            if (tresp) payload.turnstile_response = tresp;
          }

          const response = await accountRequest('code', payload);
          codeAttempts += 1;
          status.textContent = response.detail;
          if (hcaptchaWidgetId !== null && typeof window.hcaptcha !== 'undefined') {
            window.hcaptcha.reset(hcaptchaWidgetId);
          }
        } catch (error) {
          codeAttempts += 1;
          if (error.code === 'captcha_required' || error.code === 'invalid_captcha') {
            ensureHcaptcha();
            if (hcaptchaWidgetId !== null && typeof window.hcaptcha !== 'undefined') {
              window.hcaptcha.reset(hcaptchaWidgetId);
            }
          }
          status.textContent = error.detail || '验证码发送失败';
        }
        finally { busy = false; send.disabled = !options.mail_available; }
      });
      form.append(send);
      if (!options.mail_available) {
        const note = document.createElement('p'); note.className = 'account-hint'; note.textContent = '邮件服务尚未配置，请联系管理员。'; form.append(note);
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
