import {setOperatorToken, listTasks} from '../api/client.js';

export function mountOperator() {
  const dialog = document.getElementById('operator-dialog');
  const form = dialog.querySelector('form');
  const input = dialog.querySelector('input');
  const status = dialog.querySelector('[role=status]');
  const submit = dialog.querySelector('[type=submit]');
  document.getElementById('operator-login').addEventListener('click', () => {
    status.textContent = ''; input.value = ''; dialog.showModal();
  });
  dialog.querySelector('[data-close]').addEventListener('click', () => dialog.close());
  dialog.querySelector('[data-logout]').addEventListener('click', () => {
    setOperatorToken(''); window.location.reload();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true;
    setOperatorToken(input.value.trim());
    try {
      await listTasks({limit: 1});
      window.location.reload();
    } catch (error) {
      setOperatorToken('');
      status.textContent = error.status === 401 ? '令牌无效或无操作权限' : '验证失败，请稍后重试';
      submit.disabled = false;
    }
  });
}
