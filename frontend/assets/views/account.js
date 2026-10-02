import {accountRequest} from '../api/accounts.js';
import {redirectToLogin} from '../shell/operator.js';

export function mountAccount(root) {
  let disposed = false;
  const node = (tag, text) => { const el = document.createElement(tag); if (text) el.textContent = text; return el; };
  const status = node('p'); status.setAttribute('role', 'status');
  root.append(status);
  async function action(fn) {
    try { await fn(); } catch (error) { status.textContent = error.detail || '操作失败，请稍后重试'; }
  }
  function button(parent, text, fn) {
    const el = node('button', text); el.type = 'button';
    el.addEventListener('click', async () => { el.disabled = true; await action(fn); el.disabled = false; }); parent.append(el); return el;
  }
  function input(form, name, label, type = 'text') {
    const wrapper = node('label', label); const field = node('input'); field.type = type; field.name = name; field.required = true;
    if (type === 'password') { field.minLength = 12; field.maxLength = 128; field.autocomplete = name === 'old_password' ? 'current-password' : 'new-password'; }
    wrapper.append(field); form.append(wrapper); return field;
  }
  function form(parent, title, fields, submitLabel, fn) {
    const panel = node('section'); panel.className = 'panel account-panel'; panel.append(node('h2', title));
    const el = node('form'); const controls = {};
    for (const spec of fields) controls[spec[0]] = input(el, ...spec);
    const submit = node('button', submitLabel); submit.type = 'submit'; el.append(submit);
    el.addEventListener('submit', async event => {
      event.preventDefault(); submit.disabled = true;
      await action(() => fn(Object.fromEntries(Object.entries(controls).map(([key, field]) => [key, field.value]))));
      submit.disabled = false;
    }); panel.append(el); parent.append(panel); return controls;
  }
  async function load() {
    const data = await accountRequest('me'); if (disposed) return;
    root.replaceChildren(status); status.textContent = '';
    root.append(node('p', data.user.email + ' · ' + (data.user.role === 'admin' ? '管理员' : '普通用户')));
    const profile = form(root, '个人资料', [['name', '昵称']], '保存昵称', async values => {
      await accountRequest('profile', values); status.textContent = '昵称已保存';
    }); profile.name.value = data.user.name; profile.name.maxLength = 80;
    form(root, '修改密码', [['old_password', '当前密码', 'password'], ['password', '新密码', 'password'], ['confirmation', '确认新密码', 'password']], '修改密码并退出所有设备', async values => {
      if (values.password !== values.confirmation) { status.textContent = '两次输入的密码不一致'; return; }
      await accountRequest('password', values); redirectToLogin();
    });
    const sessions = node('section'); sessions.className = 'panel account-panel'; sessions.append(node('h2', '登录设备与会话'));
    for (const session of data.sessions) {
      const row = node('div'); row.className = 'account-row';
      row.append(node('span', (session.current ? '当前会话 · ' : '') + new Date(session.created * 1000).toLocaleString()));
      button(row, '退出此会话', async () => { await accountRequest('sessions/revoke', {session_id: session.id}); if (session.current) redirectToLogin(); else await load(); }); sessions.append(row);
    }
    button(sessions, '退出所有会话', async () => { await accountRequest('sessions/revoke', {}); redirectToLogin(); }); root.append(sessions);
    if (data.user.role === 'admin') await mountAdmin();
  }
  async function mountAdmin() {
    async function updateUser(user, changes) {
      try {
        await accountRequest('users/' + encodeURIComponent(user.id), {revision: user.revision, ...changes});
      } catch (error) {
        if (error.status === 409) await load();
        throw error;
      }
      await load();
    }
    form(root, '邀请用户', [['email', '受邀邮箱', 'email']], '创建邀请资格', async values => {
      const result = await accountRequest('invitations', values); status.textContent = result.detail;
    });
    const section = node('section'); section.className = 'panel account-panel'; section.append(node('h2', '账号管理')); root.append(section);
    let after = '';
    async function users() {
      const result = await accountRequest('users' + (after ? '?after=' + encodeURIComponent(after) : '')); if (disposed) return;
      for (const user of result.users) {
        const row = node('div'); row.className = 'account-row';
        row.append(node('span', user.email + ' · ' + (user.role === 'admin' ? '管理员' : '用户') + (user.active ? '' : ' · 已停用')));
        button(row, user.active ? '停用' : '启用', async () => {
          if (!window.confirm((user.active ? '停用' : '启用') + user.email + '？此操作会撤销该账号的登录会话。')) return;
          await updateUser(user, {active: !user.active});
        });
        button(row, user.role === 'admin' ? '改为普通用户' : '设为管理员', async () => {
          if (!window.confirm('确认修改 ' + user.email + ' 的权限？管理员可管理所有账号及集群功能。')) return;
          await updateUser(user, {role: user.role === 'admin' ? 'user' : 'admin'});
        }); section.append(row);
      }
      more.hidden = result.users.length < 100;
      if (result.users.length) after = result.users[result.users.length - 1].id;
    }
    const more = button(section, '加载更多账号', users); await users();
  }
  action(load);
  return () => { disposed = true; };
}
